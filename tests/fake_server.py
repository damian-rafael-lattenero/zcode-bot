#!/usr/bin/env python3
"""Servidor stdio FAKE para tests del ProtoClient.

Como el app-server real, atiende requests CONCURRENTEMENTE (un thread por
request): un request lento no bloquea a los que vienen detrás — propiedad
que /stop necesita (stop por encima de un session/send en vuelo).
Responde {"id":..,"result":{"echo":method}}; "slow_ms" simula latencia.
"""
import json
import os
import sys
import threading
import time

sys.stdout_lock = threading.Lock()


def atender(req):
    slow = (req.get("params") or {}).get("slow_ms") or 0
    if slow:
        mark = os.environ.get("ZCFAKE_SLOW_MARK")
        if mark:
            # señal para el test: el request lento YA está en vuelo acá
            with open(mark, "w") as f:
                f.write(req.get("method") or "?")
        time.sleep(slow / 1000.0)
    pad = (req.get("params") or {}).get("pad_bytes") or 0
    result = {"echo": req.get("method")}
    if pad:
        result["pad"] = "x" * int(pad)  # frame GRANDE: test del max_frame
    frame = json.dumps({"id": req.get("id"), "result": result})
    # sin \n final: fuerza el fallback de 512KB del reader (test F13)
    sep = "" if (req.get("params") or {}).get("no_newline") else "\n"
    with sys.stdout_lock:
        sys.stdout.write(frame + sep)
        sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except ValueError:
        continue
    threading.Thread(target=atender, args=(req,), daemon=True).start()
