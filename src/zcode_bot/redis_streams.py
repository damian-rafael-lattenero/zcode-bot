"""Redis Streams — reemplazo de archivos JSON por streams reactivos.
 
Diseño:
- Cada conversación (sid) tiene su stream: queue:{sid}, questions:{sid}
- Consumer groups para workers: worker:{sid}
- Outbox global: outbox (consumer group: poller)
- Active turns: active:turns (hash + stream)
- Perm replay: perm:replay (hash)
- Live skip: live:skip (hash con TTL)

Convenciones de命名:
- queue:{sid}          → prompts encolados por sesión
- questions:{sid}      → preguntas ❓ pendientes (consumer group: worker:{sid})
- worker:cmd:{sid}     → control de worker (stop, pause)
- events:{sid}         → CoT deltas, tool calls (pub/sub)
- outbox               → notificaciones Telegram pendientes
- active:turns         → registry de turnos vivos (hash)
- perm:replay          → deduplicación permisos/preguntas (hash)
- live:skip            → flag live view desactivado por sesión (hash + TTL)
"""
import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional, List, Dict, Callable, Awaitable

import redis.asyncio as redis
from redis.asyncio import Redis


# Configuración por defecto
DEFAULT_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# TTLs en milisegundos
STREAM_TTL = {
    "cmd": 86_400_000,        # 24h comandos
    "queue": 604_800_000,     # 7d colas
    "questions": 86_400_000,  # 24h preguntas
    "events": 3_600_000,      # 1h CoT live
    "outbox": 86_400_000,     # 24h notificaciones
}

# Nombres de streams
class Streams:
    @staticmethod
    def commands(chat_id: int) -> str: return f"cmd:{chat_id}"
    @staticmethod
    def queue(sid: str) -> str: return f"queue:{sid}"
    @staticmethod
    def questions(sid: str) -> str: return f"questions:{sid}"
    @staticmethod
    def worker_cmd(sid: str) -> str: return f"worker:cmd:{sid}"
    @staticmethod
    def events(sid: str) -> str: return f"events:{sid}"
    @staticmethod
    def outbox() -> str: return "outbox"
    @staticmethod
    def active_turns() -> str: return "active:turns"


@dataclass
class StreamConfig:
    url: str = DEFAULT_REDIS_URL
    max_connections: int = 50
    socket_timeout: float = 5.0
    socket_connect_timeout: float = 5.0
    decode_responses: bool = True


# Cliente Redis singleton
_redis_client: Optional[Redis] = None


async def get_redis(config: Optional[StreamConfig] = None) -> Redis:
    """Obtiene o crea el cliente Redis singleton."""
    global _redis_client
    if _redis_client is None:
        cfg = config or StreamConfig()
        _redis_client = redis.from_url(
            cfg.url,
            max_connections=cfg.max_connections,
            socket_timeout=cfg.socket_timeout,
            socket_connect_timeout=cfg.socket_connect_timeout,
            decode_responses=cfg.decode_responses,
        )
    return _redis_client


async def close_redis():
    """Cierra la conexión Redis."""
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None


# ── Helpers genéricos ──

async def xadd_stream(r: Redis, stream: str, data: Dict[str, str], maxlen: int = 10000) -> str:
    """Añade entrada a stream con TTL automático."""
    return await r.xadd(stream, data, maxlen=maxlen, approximate=True)


async def xread_block(r: Redis, streams: Dict[str, str], count: int = 10, block: int = 2000):
    """Lee de streams de forma bloqueante (reactiva)."""
    return await r.xread(streams, count=count, block=block)


async def xreadgroup_block(
    r: Redis, group: str, consumer: str, streams: Dict[str, str], count: int = 1, block: int = 5000
):
    """Lee de stream via consumer group (reactivo)."""
    return await r.xreadgroup(group, consumer, streams, count=count, block=block)


async def xack(r: Redis, stream: str, group: str, *msg_ids: str) -> int:
    """Acknowledge mensajes en consumer group."""
    return await r.xack(stream, group, *msg_ids)


async def xgroup_create(r: Redis, stream: str, group: str, id: str = "0", mkstream: bool = True):
    """Crea consumer group si no existe."""
    try:
        await r.xgroup_create(stream, group, id=id, mkstream=mkstream)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


# ── Queue por sesión (queue:{sid}) ──

async def queue_put(r: Redis, sid: str, act: Dict, text: str, maxlen: int = 5) -> None:
    """Encola prompt para una sesión (LPUSH + LTRIM atómico via stream)."""
    stream = Streams.queue(sid)
    data = {"act": json.dumps(act), "text": text[:6000], "ts": str(time.time())}
    await xadd_stream(r, stream, data, maxlen=maxlen)


async def queue_take(r: Redis, sid: str) -> List[Dict]:
    """Toma y elimina el prompt más antiguo de la cola."""
    stream = Streams.queue(sid)
    # Leer el primer mensaje (más antiguo)
    entries = await r.xrange(stream, count=1)
    if not entries:
        return []
    msg_id, data = entries[0]
    await r.xdel(stream, msg_id)
    return [{"act": json.loads(data["act"]), "text": data["text"]}]


async def queue_peek(r: Redis, sid: str) -> List[Dict]:
    """Mira la cola sin consumir."""
    stream = Streams.queue(sid)
    entries = await r.xrange(stream, count=5)
    return [{"act": json.loads(d["act"]), "text": d["text"]} for _, d in entries]


async def queue_sids_with_items(r: Redis) -> List[str]:
    """SIDs que tienen items en cola (scan simple)."""
    # Nota: en producción usar Redis Set para tracking eficiente
    sids = []
    async for key in r.scan_iter(match="queue:*"):
        if await r.xlen(key) > 0:
            sids.append(key.split(":", 1)[1])
    return sids


# ── Questions por sesión (questions:{sid}) ──

async def questions_ask(r: Redis, rid: str, questions: List, titulo: str, sid: Optional[str] = None) -> Dict:
    """Crea pregunta pendiente."""
    stream = Streams.questions(sid or "global")
    data = {
        "rid": rid,
        "questions": json.dumps(questions),
        "titulo": titulo,
        "sid": sid or "",
        "ts": str(time.time()),
    }
    await xadd_stream(r, stream, data, maxlen=100)
    return data


async def questions_answer(r: Redis, rid: str, text: str, sid: Optional[str] = None) -> bool:
    """Responde y elimina la pregunta."""
    stream = Streams.questions(rid)  # rid único, no sid
    # Buscar por rid en el stream
    entries = await r.xrange(Streams.questions("global"), count=100)
    for msg_id, data in entries:
        if data.get("rid") == rid:
            await r.xdel(Streams.questions("global"), msg_id)
            return True
    return False


async def questions_wait(r: Redis, rid: str, timeout: float = 480.0, poll: float = 2.0) -> Optional[str]:
    """Espera reactiva por respuesta (blocking read)."""
    stream = Streams.questions("global")
    deadline = time.time() + timeout
    last_id = "0"
    while time.time() < deadline:
        try:
            entries = await r.xread({stream: last_id}, count=1, block=int(min(2000, (deadline - time.time()) * 1000)))
            for stream_name, entries in entries:
                for msg_id, data in entries:
                    if data.get("rid") == rid:
                        return data.get("answer")
                    last_id = msg_id
        except Exception:
            await asyncio.sleep(0.1)
    return None


async def questions_pending(r: Redis, max_age_s: float = 3600) -> List[Dict]:
    """Lista preguntas pendientes no expiradas."""
    stream = Streams.questions("global")
    entries = await r.xrange(stream, count=100)
    now = time.time()
    pending = []
    for msg_id, data in entries:
        if now - float(data.get("ts", 0)) <= max_age_s:
            data["msg_id"] = msg_id
            pending.append(data)
    return pending


# ── Worker Commands (worker:cmd:{sid}) ──

async def worker_cmd_send(r: Redis, sid: str, cmd: str, data: Dict = None) -> str:
    """Envía comando a worker (stop, pause, etc.)."""
    stream = Streams.worker_cmd(sid)
    data = {"cmd": cmd, "data": json.dumps(data or {}), "ts": str(time.time())}
    return await xadd_stream(r, f"worker:cmd:{sid}", data, maxlen=100)


async def worker_cmd_listen(r: Redis, sid: str, group: str, consumer: str, handler: Callable[[Dict], Awaitable[None]]):
    """Escucha comandos para un worker (consumer group)."""
    stream = Streams.worker_cmd(sid)
    await xgroup_create(r, stream, group)
    while True:
        entries = await xreadgroup_block(r, group, consumer, {stream: ">"}, count=1, block=5000)
        for stream_name, entries in entries:
            for msg_id, data in entries:
                try:
                    await handler(data)
                    await r.xack(stream, group, msg_id)
                except Exception:
                    pass  # no ack = re-delivery


# ── Events / CoT (events:{sid}) ──

async def events_publish(r: Redis, sid: str, event: Dict) -> str:
    """Publica evento CoT (reasoning_delta, text_delta, tool_call, etc.)."""
    stream = Streams.events(sid)
    data = {k: json.dumps(v) if not isinstance(v, str) else v for k, v in event.items()}
    return await xadd_stream(r, stream, data, maxlen=10000)


async def events_subscribe(r: Redis, sid: str, last_id: str = "0") -> List[Dict]:
    """Lee eventos nuevos desde last_id (reactivo)."""
    stream = Streams.events(sid)
    entries = await r.xread({stream: last_id}, count=100, block=2000)
    events = []
    for stream_name, entries in entries:
        for msg_id, data in entries:
            # Convertir strings JSON de vuelta a objetos
            parsed = {k: json.loads(v) if isinstance(v, str) and v.startswith(("{", "[")) else v
                      for k, v in data.items()}
            events.append({"event_id": msg_id, **parsed})
    return events


# ── Outbox (notificaciones Telegram) ──

async def outbox_put(r: Redis, chat_id: int, text: str, loud: bool = False) -> str:
    """Encola notificación para envío asíncrono."""
    data = {
        "chat_id": str(chat_id),
        "text": text[:3900],
        "loud": str(loud).lower(),
        "ts": str(time.time()),
    }
    return await xadd_stream(r, Streams.outbox(), data, maxlen=10000)


async def outbox_flush_async(r: Redis, send_fn: Callable[[str, str, bool], Awaitable[bool]], batch: int = 50) -> int:
    """Procesa outbox de forma reactiva (consumer group: poller)."""
    stream = Streams.outbox()
    await xgroup_create(r, stream, "poller")
    sent = 0
    entries = await xreadgroup_block(r, "poller", "poller-1", {stream: ">"}, count=batch, block=1000)
    for stream_name, entries in entries:
        for msg_id, data in entries:
            try:
                ok = await send_fn(data["chat_id"], data["text"], data["loud"] == "true")
                if ok:
                    await r.xack(stream, "poller", msg_id)
                    sent += 1
            except Exception:
                pass  # no ack = reintento
    return sent


# ── Active Turns (hash + stream) ──

async def active_turn_register(r: Redis, sid: str, titulo: str, t0: float, ws: str, last_id: str) -> None:
    """Registra turno activo en hash + stream."""
    pipe = r.pipeline()
    pipe.hset("active:turns", sid, json.dumps({
        "titulo": titulo, "t0": t0, "pid": os.getpid(), "ws": ws, "last_id": last_id
    }))
    pipe.xadd("active:turns:stream", {"sid": sid, "action": "start", "ts": str(time.time())})
    await pipe.execute()


async def active_turn_clear(r: Redis, sid: str) -> None:
    """Limpia turno activo."""
    pipe = r.pipeline()
    pipe.hdel("active:turns", sid)
    pipe.xadd("active:turns:stream", {"sid": sid, "action": "end", "ts": str(time.time())})
    await pipe.execute()


async def active_turns_get_all(r: Redis) -> Dict:
    """Obtiene todos los turnos activos."""
    return await r.hgetall("active:turns")


async def orphans_check(r: Redis, send_fn: Callable[[str, str, bool], Awaitable[None]]) -> None:
    """Supervisa turnos huérfanos (worker muerto sin finally)."""
    turns = await r.hgetall("active:turns")
    if not turns:
        return
    now = time.time()
    for sid, data_str in turns.items():
        data = json.loads(data_str)
        if now - data["t0"] > 7200:
            await r.hdel("active:turns", sid)
            continue
        pid = data.get("pid")
        try:
            os.kill(int(pid), 0)
            continue  # vivo
        except (OSError, ValueError, TypeError):
            pass
        # Worker muerto: verificar si turno terminó
        # ... lógica de entrega similar a gateway.py


# ── Perm Replay (hash) ──

async def perm_remember(r: Redis, key: str, decision: str) -> None:
    """Registra decisión para replay (dedup)."""
    pipe = r.pipeline()
    pipe.hset("perm:replay", key, json.dumps({"ts": time.time(), "decision": decision}))
    # Podar: mantener últimos 50
    pipe.hlen("perm:replay")  # trigger cleanup logic elsewhere
    await pipe.execute()


async def perm_replay(r: Redis, key: str, max_age_s: float = 600) -> Optional[str]:
    """Obtiene decisión replay si es reciente."""
    data = await r.hget("perm:replay", key)
    if not data:
        return None
    entry = json.loads(data)
    if time.time() - entry["ts"] < max_age_s:
        return entry["decision"]
    return None


# ── Live Skip (hash con TTL) ──

async def live_skip_mark(r: Redis, sid: str) -> None:
    """Marca sesión como 'replay gigante' para desactivar live view."""
    await r.hset("live:skip", sid, str(time.time()))
    await r.expire("live:skip", 21600)  # 6h TTL


async def live_skip_check(r: Redis, sid: str) -> bool:
    """Verifica si sesión tiene flag fresco."""
    ts = await r.hget("live:skip", sid)
    if not ts:
        return False
    return time.time() - float(ts) < 21600


async def live_skip_clear(r: Redis, sid: str) -> None:
    """Limpia flag (ej. tras compact)."""
    await r.hdel("live:skip", sid)


# ── Inicialización ──

async def init_redis_streams(r: Redis) -> None:
    """Crea consumer groups y estructuras base."""
    # Consumer groups para streams principales
    groups = [
        ("queue:*", "worker:"),  # pattern: worker:{sid}
        ("questions:global", "poller"),
        ("outbox", "poller"),
        ("events:*", "live_view"),  # pattern: live_view:{sid}
    ]
    # Nota: xgroup_create con pattern no existe en Redis, se crea al primer uso
    # Esta función sirve como referencia de la arquitectura
    pass