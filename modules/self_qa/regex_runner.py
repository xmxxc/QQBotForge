"""Bounded regular-expression search in one killable worker process per Bot.

Python ``re`` cannot be interrupted, so a catastrophic pattern would hold the
event loop (or a thread) indefinitely. Each Bot gets its own ``python -I``
worker speaking JSON lines; a request that exceeds the time limit kills only
that Bot's worker, its regex rules count as not matched, and a fresh worker
starts on the next request.
"""

import asyncio
import json
import re
import sys

TIMEOUT = 0.5
MAX_PATTERN = 500
MAX_TEXT = 2000
WORKER = r"""
import json, re, sys
cache = {}
for line in sys.stdin:
    request = json.loads(line)
    if len(cache) > 1000:
        cache.clear()
    found = []
    for pattern in request['patterns']:
        compiled = cache.get(pattern)
        if compiled is None:
            compiled = cache[pattern] = re.compile(pattern)
        found.append(compiled.search(request['text']) is not None)
    sys.stdout.write(json.dumps(found) + '\n')
    sys.stdout.flush()
"""


def compile_check(pattern):
    """Validation at save time; raises ValueError with feedback for the owner."""
    if len(pattern) > MAX_PATTERN:
        raise ValueError(f'正则最多 {MAX_PATTERN} 字符')
    try:
        re.compile(pattern)
    except re.error as error:
        raise ValueError(f'正则无效：{pattern}（{error}）') from None


class RegexRunner:
    def __init__(self, *, timeout=TIMEOUT):
        self.timeout = timeout
        self.workers = {}
        self.locks = {}
        self.closed = False

    async def _worker(self, bot):
        process = self.workers.get(bot)
        if process is None or process.returncode is not None:
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-I', '-c', WORKER, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            self.workers[bot] = process
        return process

    def _kill(self, bot):
        process = self.workers.pop(bot, None)
        if process is not None and process.returncode is None:
            process.kill()

    async def search(self, bot, patterns, text):
        """Return one bool per pattern, or None when the limit was exceeded or the worker failed."""
        if not patterns:
            return []
        if self.closed:
            return None
        request = (json.dumps({'patterns': patterns, 'text': text[:MAX_TEXT]}) + '\n').encode()
        lock = self.locks.setdefault(bot, asyncio.Lock())
        try:
            # Waiting behind another request never kills that request's worker.
            await asyncio.wait_for(lock.acquire(), self.timeout)
        except TimeoutError:
            return None
        try:
            process = await self._worker(bot)
            process.stdin.write(request)
            await process.stdin.drain()
            found = json.loads(await asyncio.wait_for(process.stdout.readline(), self.timeout))
            if not isinstance(found, list) or len(found) != len(patterns):
                raise ValueError('regex worker response')
            return found
        except (TimeoutError, OSError, ValueError):
            self._kill(bot)
            return None
        except asyncio.CancelledError:
            self._kill(bot)
            raise
        finally:
            lock.release()

    def close(self):
        self.closed = True
        for bot in list(self.workers):
            self._kill(bot)
