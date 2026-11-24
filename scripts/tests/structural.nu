# structural.nu — the retired Python bot's leftovers (bootstrap, deploy,
# bot-revive): kept for history, still on disk, still runnable. Generic proof:
# parse + shebang resolves + no stale import.
# covers: /Users/lattenero/Projects/bot/*.nu
# covers: /Users/lattenero/Projects/bot/scripts/*.nu
use std/assert
use /Users/lattenero/Scripts/lib/structural.nu *

let files: list<string> = ((glob '/Users/lattenero/Projects/bot/*.nu') | append (glob '/Users/lattenero/Projects/bot/scripts/*.nu'))
assert greater ($files | length) 2 "the glob must see the family"

let found: list<Finding> = (findings $files)
assert equal $found [] ($found | to json)

print $"bot retired structural ok — ($files | length) scripts"
