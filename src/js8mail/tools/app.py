"""Run the local JS8Mail mailbox and daemon.

The first UI slice supports durable compose/cancel/retry and explicitly
operator-approved transmission. Automatic routing and end-to-end receipts are
not enabled yet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from js8mail.adapters.js8call.client import Js8CallClient
from js8mail.application.lifecycle import MessageState
from js8mail.application.service import MailService
from js8mail.domain import NormalizedEvent
from js8mail.storage import Database

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>JS8Mail</title><style>
body{font:15px system-ui;max-width:1100px;margin:2em auto;padding:0 1em;background:#f5f7f9;color:#18222d}
section{background:white;border:1px solid #d9e0e7;border-radius:10px;padding:1em;margin:1em 0}input,textarea,select{box-sizing:border-box;width:100%;padding:.5em;margin:.25em 0 .7em}textarea{height:110px}button{background:#1769aa;color:#fff;border:0;border-radius:5px;padding:.5em .8em;margin:.2em;cursor:pointer}.danger{background:#a33}.pill{display:inline-block;padding:.3em .6em;border-radius:1em;background:#e8edf2;margin:.2em}.ok{background:#d8f3dc}.warn{background:#fff1c2}.mono{font:12px monospace;white-space:pre-wrap}td,th{text-align:left;border-bottom:1px solid #e4e9ee;padding:.5em;vertical-align:top}
</style><h1>JS8Mail</h1><p>Offline-first mailbox · operator-approved RF prototype</p><section><div id=status>Loading…</div></section>
<section><h2>Compose</h2><form id=compose>Destination<input name=destination maxlength=16 required placeholder=N0CALL>Subject<input name=subject maxlength=120>Message<textarea name=body maxlength=4096 required></textarea>Priority<select name=priority><option value=0>Normal</option><option value=1>High</option><option value=2>Urgent</option><option value=3>Emergency</option></select><button>Queue locally</button></form><span id=result></span></section>
<section><h2>Outbox</h2><div id=messages>Loading…</div></section><section><h2>Recent observations</h2><div id=observations>Loading…</div></section>
<script>
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(u,o){let r=await fetch(u,o),j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}
async function refresh(){let s=await api('/api/status');status.innerHTML=`<span class='pill ${s.connected?'ok':'warn'}'>JS8Call: ${s.connected?'connected':'offline'}</span><span class=pill>TX mode: ${s.tx_mode}</span><span class=pill>Port: ${s.port}</span>`;let m=await api('/api/messages');messages.innerHTML=m.length?'<table><tr><th>State</th><th>To</th><th>Content</th><th>Action</th></tr>'+m.map(x=>`<tr><td>${esc(x.state)}<br><small>${esc(x.id)}</small></td><td>${esc(x.destination)}</td><td>${esc(x.subject)}<br>${esc(x.body)}</td><td>${s.tx_mode==='approve'&&['queued','waiting_route'].includes(x.state)?`<button onclick="act('${x.id}','send')">Approve &amp; send</button>`:''}${['queued','waiting_route'].includes(x.state)?`<button class=danger onclick="act('${x.id}','cancel')">Cancel</button>`:''}${['failed','cancelled'].includes(x.state)?`<button onclick="act('${x.id}','retry')">Retry</button>`:''}</td></tr>`).join('')+'</table>':'<p>No messages.</p>';let o=await api('/api/observations');observations.innerHTML=o.map(x=>`<div class=mono>${new Date(x.observed_at_ms).toLocaleTimeString()} ${esc(x.event_type)} ${esc(x.value)}</div>`).join('')||'<p>Waiting for JS8Call events.</p>'}
async function act(id,a){try{await api(`/api/messages/${id}/${a}`,{method:'POST'});refresh()}catch(e){alert(e)}}
compose.onsubmit=async e=>{e.preventDefault();try{let x=await api('/api/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});result.textContent='Queued '+x.id;e.target.reset();refresh()}catch(e){result.textContent=e}}
refresh();setInterval(refresh,3000);
</script>"""


class Handler(BaseHTTPRequestHandler):
    service: MailService
    client: Js8CallClient
    loop: asyncio.AbstractEventLoop
    status: dict[str, Any]

    def reply(self, code: int, value: Any, content_type: str = "application/json") -> None:
        data = value.encode() if isinstance(value, str) else json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.reply(200, PAGE, "text/html")
        elif path == "/api/status":
            self.reply(200, self.status)
        elif path == "/api/messages":
            self.reply(200, self.service.database.list_messages())
        elif path == "/api/observations":
            self.reply(200, self.service.database.recent_observations())
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 10000:
                raise ValueError("request too large")
            payload = json.loads(self.rfile.read(size)) if size else {}
            if path == "/api/messages":
                message_id = self.service.compose(
                    str(payload.get("destination", "")),
                    str(payload.get("subject", "")),
                    str(payload.get("body", "")),
                    int(payload.get("priority", 0)),
                )
                self.reply(201, {"id": message_id})
                return
            parts = path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "messages"]:
                raise ValueError("not found")
            message_id, action = parts[2], parts[3]
            if action == "cancel":
                self.service.cancel(message_id)
            elif action == "retry":
                self.service.retry(message_id)
            elif action == "send":
                if self.status["tx_mode"] != "approve":
                    raise ValueError("transmission approval is disabled")
                future = asyncio.run_coroutine_threadsafe(self.transmit(message_id), self.loop)
                future.result(timeout=30)
            else:
                raise ValueError("unknown action")
            self.reply(200, {"ok": True})
        except (ValueError, KeyError, json.JSONDecodeError, TimeoutError, ConnectionError) as exc:
            self.reply(400, {"error": str(exc)})

    async def transmit(self, message_id: str) -> None:
        message = self.service.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        if message["state"] not in {MessageState.QUEUED, MessageState.WAITING_ROUTE}:
            raise ValueError("message is not ready to send")
        # Keep the first vertical slice ordinary-JS8Call compatible. Enhanced
        # envelopes will be added later, behind peer capability detection.
        text = f"{message['destination']} MSG {message['body']}"
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.transition_message(message_id, MessageState.IN_PROGRESS)
        await self.client.send_message(text)
        self.service.database.audit(
            "message.submitted_to_js8call", {"message_id": message_id, "text_length": len(text)}
        )

    def log_message(self, format: str, *args: object) -> None:
        return


async def run(args: argparse.Namespace) -> None:
    database = Database(Path(args.database).expanduser())
    service = MailService(database)
    client = Js8CallClient(args.host, args.port)
    loop = asyncio.get_running_loop()
    status: dict[str, Any] = {
        "connected": False,
        "host": args.host,
        "port": args.port,
        "tx_mode": args.tx_mode,
    }
    handler = type(
        "BoundHandler",
        (Handler,),
        {"service": service, "client": client, "loop": loop, "status": status},
    )
    server = ThreadingHTTPServer((args.ui_host, args.ui_port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"JS8Mail UI: http://{args.ui_host}:{args.ui_port}", flush=True)
    delay = 1.0
    try:
        while True:
            try:
                await client.connect()
                status["connected"] = True
                database.audit("js8call.connected", {"host": args.host, "port": args.port})
                delay = 1.0

                async def handle(event: NormalizedEvent) -> None:
                    database.record_observation(event)

                await client.read_events(handle)
            except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
                status["connected"] = False
                database.audit("js8call.connection_error", {"error": type(exc).__name__})
            finally:
                status["connected"] = False
                await client.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    finally:
        server.shutdown()
        server.server_close()
        database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=2442, type=int)
    parser.add_argument("--database", default="js8mail.sqlite3")
    parser.add_argument("--ui-host", default="127.0.0.1")
    parser.add_argument("--ui-port", default=8765, type=int)
    parser.add_argument("--tx-mode", choices=("observe", "approve"), default="observe")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
