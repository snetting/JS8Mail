"""Run the local JS8Mail mailbox and daemon."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from js8mail.adapters.js8call.client import Js8CallClient
from js8mail.application.lifecycle import MessageState
from js8mail.application.service import MailService
from js8mail.discovery import QueryScheduler, call_query, hearing_query, messages_query, snr_query
from js8mail.domain import NormalizedEvent, utc_now_ms
from js8mail.protocol import (
    MessagePart,
    MultipartAccumulator,
    format_delivery_ack,
    format_human_data_part,
    format_ordinary_message,
    format_part_ack,
    parse_ack,
    parse_delivery_ack,
    parse_part_ack,
    split_human_message,
)
from js8mail.storage import Database

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>JS8Mail</title><style>
body{font:15px system-ui;max-width:1100px;margin:2em auto;padding:0 1em;background:#f5f7f9;color:#18222d}
section{background:white;border:1px solid #d9e0e7;border-radius:10px;padding:1em;margin:1em 0}input,textarea,select{box-sizing:border-box;width:100%;padding:.5em;margin:.25em 0 .7em}textarea{height:110px}button{background:#1769aa;color:#fff;border:0;border-radius:5px;padding:.5em .8em;margin:.2em;cursor:pointer}.danger{background:#a33}.pill{display:inline-block;padding:.3em .6em;border-radius:1em;background:#e8edf2;margin:.2em}.ok{background:#d8f3dc}.warn{background:#fff1c2}.mono{font:12px monospace;white-space:pre-wrap}td,th{text-align:left;border-bottom:1px solid #e4e9ee;padding:.5em;vertical-align:top}
</style><h1>JS8Mail</h1><p>Offline-first mailbox · automatic RF handoff prototype</p><section><div id=status>Loading…</div></section>
<section><h2>Compose</h2><form id=compose>Destination<input name=destination maxlength=16 required placeholder=N0CALL>Subject<input name=subject maxlength=120>Message<textarea name=body maxlength=4096 required></textarea>Priority<select name=priority><option value=0>Normal</option><option value=1>High</option><option value=2>Urgent</option><option value=3>Emergency</option></select><button>Queue locally</button></form><span id=result></span></section>
<section><h2>Outbox</h2><div id=messages>Loading…</div></section><section><h2>Recent observations</h2><div id=observations>Loading…</div></section>
<section><h2>Live route preview</h2><p>Uses only locally captured RF evidence. The graph is rebuilt as observations arrive.</p><form id=route>Origin<input name=origin maxlength=16 required placeholder=OH3SPN>Destination<input name=destination maxlength=16 required placeholder=G0XYZ><button>Preview route</button></form><div id=route-result>No route selected.</div></section>
<script>
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let routeQuery='';
async function api(u,o){let r=await fetch(u,o),j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}
async function refresh(){let s=await api('/api/status');document.getElementById('status').innerHTML=`<span class='pill ${s.connected?'ok':'warn'}'>JS8Call: ${s.connected?'connected':'offline'}</span><span class=pill>Station: ${esc(s.callsign||'unknown')}</span><span class=pill>TX mode: ${s.tx_mode}</span><span class=pill>Port: ${s.port}</span>`;if(s.callsign&&!document.querySelector('#route input[name=origin]').value)document.querySelector('#route input[name=origin]').value=s.callsign;let m=await api('/api/messages');document.getElementById('messages').innerHTML=m.length?'<table><tr><th>State / timeline</th><th>To</th><th>Content</th><th>Action</th></tr>'+m.map(x=>`<tr><td><b>${esc(x.state)}</b><br><small>${esc(x.id)}</small>${x.next_attempt_at_ms?`<div class=mono>next retry: ${new Date(x.next_attempt_at_ms).toLocaleTimeString()} (attempt ${x.retry_count})</div>`:''}${(x.attempts||[]).map(a=>`<div class=mono>${esc(a.action)} → ${esc(a.target)}: ${esc(a.status)}${a.detail?' · '+esc(a.detail):''}</div>`).join('')}</td><td>${esc(x.destination)}</td><td>${esc(x.subject)}<br>${esc(x.body)}</td><td>${['queued','waiting_route'].includes(x.state)?`<button class=danger onclick="act('${x.id}','cancel')">Cancel</button>`:''}${['failed','cancelled'].includes(x.state)?`<button onclick="act('${x.id}','retry')">Retry</button>`:''}</td></tr>`).join('')+'</table>':'<p>No messages.</p>';let o=await api('/api/observations');document.getElementById('observations').innerHTML=o.map(x=>`<div class=mono>${new Date(x.observed_at_ms).toLocaleTimeString()} ${esc(x.event_type)} ${esc(x.value)}</div>`).join('')||'<p>Waiting for JS8Call events.</p>';if(routeQuery){let r=await api('/api/route?'+routeQuery);document.getElementById('route-result').innerHTML=`<p><b>${esc(r.action)}</b>: ${esc(r.explanation)}</p><p class=mono>${esc(r.path.join(' → '))}</p>`}}
async function act(id,a){try{await api(`/api/messages/${id}/${a}`,{method:'POST'});refresh()}catch(e){alert(e)}}
document.getElementById('compose').onsubmit=async e=>{e.preventDefault();try{let x=await api('/api/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});document.getElementById('result').textContent='Queued '+x.id;e.target.reset();refresh()}catch(e){document.getElementById('result').textContent=e}}
document.getElementById('route').onsubmit=e=>{e.preventDefault();let f=new FormData(e.target);routeQuery=new URLSearchParams({origin:f.get('origin'),destination:f.get('destination')});refresh()}
refresh();setInterval(refresh,3000);
</script>"""


class Handler(BaseHTTPRequestHandler):
    service: MailService
    client: Js8CallClient
    loop: asyncio.AbstractEventLoop
    status: dict[str, Any]
    announced_destinations: set[str]

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
            self.reply(200, self.service.message_views())
        elif path == "/api/observations":
            self.reply(200, self.service.database.recent_observations(12))
        elif path == "/api/route":
            query = parse_qs(urlparse(self.path).query)
            origin = query.get("origin", [""])[0]
            destination = query.get("destination", [""])[0]
            if not origin or not destination:
                self.reply(400, {"error": "origin and destination are required"})
            else:
                self.reply(200, asdict(self.service.plan_route(origin, destination)))
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
                if self.status["tx_mode"] == "automatic":
                    future = asyncio.run_coroutine_threadsafe(self.prepare(message_id), self.loop)
                    future.result(timeout=30)
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
        destination = str(message["destination"])
        announce = destination not in self.announced_destinations
        text = format_ordinary_message(destination, str(message["body"]), announce)
        self.service.database.record_attempt(
            message_id, "direct", destination, "started", "initial direct attempt"
        )
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.transition_message(message_id, MessageState.IN_PROGRESS)
        try:
            await self.client.send_message(text)
        except (ConnectionError, OSError, RuntimeError) as exc:
            self.service.database.record_attempt(
                message_id, "direct", destination, "failed", type(exc).__name__
            )
            raise
        self.service.database.record_attempt(
            message_id,
            "direct",
            destination,
            "submitted",
            "queued in JS8Call for next TX cycle",
        )
        self.service.database.audit(
            "message.submitted_to_js8call", {"message_id": message_id, "text_length": len(text)}
        )
        self.announced_destinations.add(destination)

    async def prepare(self, message_id: str) -> None:
        """Probe an unobserved destination before committing message airtime."""
        message = self.service.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        destination = str(message["destination"])
        if self.service.recently_heard(destination):
            await self.transmit(message_id)
            return
        probe = snr_query(destination)
        self.service.database.record_attempt(
            message_id, "snr_probe", destination, "started", "destination not recently heard"
        )
        try:
            await self.client.send_message(probe)
        except (ConnectionError, OSError, RuntimeError) as exc:
            self.service.database.record_attempt(
                message_id, "snr_probe", destination, "failed", type(exc).__name__
            )
        else:
            self.service.database.record_attempt(
                message_id, "snr_probe", destination, "submitted", "waiting for RF evidence"
            )
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.defer_message(
            message_id, 60_000, "probe first; retry discovery in 1 minute(s)"
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
        "callsign": "",
    }
    handler: type[Handler] = type(
        "BoundHandler",
        (Handler,),
        {
            "service": service,
            "client": client,
            "loop": loop,
            "status": status,
            "announced_destinations": set(),
        },
    )
    server = ThreadingHTTPServer((args.ui_host, args.ui_port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"JS8Mail UI: http://{args.ui_host}:{args.ui_port}", flush=True)
    delay = 1.0
    query_scheduler = QueryScheduler()
    inbox_scheduler = QueryScheduler(base_delay_ms=1_800_000, max_delay_ms=21_600_000)
    reassembly: dict[str, MultipartAccumulator] = {}

    async def submit_query(
        key: str,
        text: str,
        action: str,
        target: str,
        scheduler: QueryScheduler = query_scheduler,
    ) -> bool:
        now = int(asyncio.get_running_loop().time() * 1000)
        if not client.connected or not scheduler.due(key, now):
            return False
        try:
            await client.send_message(text)
            database.audit(
                "discovery.query_submitted", {"action": action, "target": target, "text": text}
            )
            scheduler.record(key, now)
            return True
        except (ConnectionError, RuntimeError):
            scheduler.record(key, now)
            return False

    async def discovery_loop() -> None:
        inbox_key = "inbox:broadcast"
        while True:
            await asyncio.sleep(5)
            if not client.connected or args.tx_mode != "automatic":
                continue
            now = int(asyncio.get_running_loop().time() * 1000)
            if inbox_scheduler.due(inbox_key, now):
                await submit_query(
                    inbox_key,
                    messages_query(),
                    "messages_query",
                    "@ALLCALL",
                    scheduler=inbox_scheduler,
                )
            for message in database.list_messages():
                if message["state"] not in {MessageState.IN_PROGRESS, MessageState.WAITING_ROUTE}:
                    continue
                destination = str(message["destination"])
                expires_at_ms = message.get("expires_at_ms")
                if isinstance(expires_at_ms, int) and expires_at_ms <= utc_now_ms():
                    database.transition_message(str(message["id"]), MessageState.EXPIRED)
                    database.record_attempt(
                        str(message["id"]), "expiry", destination, "expired", "retry window elapsed"
                    )
                    continue
                if service.recently_heard(destination):
                    if message["state"] == MessageState.WAITING_ROUTE:
                        database.record_attempt(
                            str(message["id"]), "route", destination, "available", "recent local RF evidence"
                        )
                        try:
                            future = asyncio.create_task(
                                handler.transmit(cast(Handler, handler), str(message["id"]))
                            )
                            await future
                        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                            database.record_attempt(
                                str(message["id"]),
                                "direct",
                                destination,
                                "deferred",
                                f"probe/queue still busy: {type(exc).__name__}",
                            )
                            database.defer_message(
                                str(message["id"]),
                                60_000,
                                "route known but JS8Call TX slot was still occupied",
                            )
                    continue
                promising = service.promising_stations(destination)[:3]
                if (
                    message["state"] == MessageState.WAITING_ROUTE
                    and not database.due_for_retry(str(message["id"]))
                    and not promising
                ):
                    continue
                hearing_key = f"hearing:{destination}"
                if query_scheduler.due(hearing_key, now):
                    hearing_submitted = await submit_query(
                        hearing_key, hearing_query(destination), "hearing_query", destination
                    )
                    database.record_attempt(
                        str(message["id"]),
                        "hearing_query",
                        destination,
                        "submitted" if hearing_submitted else "blocked",
                        "recent evidence absent" if hearing_submitted else "JS8Call TX slot unavailable or query cooldown",
                    )
                state = query_scheduler.state(hearing_key)
                call_key = f"call-query:{destination}"
                if state.attempts >= 1 and query_scheduler.due(call_key, now):
                    candidates = promising
                    if candidates:
                        for candidate in candidates:
                            candidate_key = f"candidate-query:{candidate}:{destination}"
                            if query_scheduler.due(candidate_key, now):
                                candidate_submitted = await submit_query(
                                    candidate_key,
                                    f"{candidate} QUERY CALL {destination}",
                                    "candidate_query_call",
                                    candidate,
                                )
                                database.record_attempt(
                                    str(message["id"]),
                                    "candidate_query_call",
                                    candidate,
                                    "submitted" if candidate_submitted else "blocked",
                                    destination,
                                )
                    else:
                        allcall_submitted = await submit_query(
                            call_key, call_query(destination), "allcall_query_call", "@ALLCALL"
                        )
                        database.record_attempt(
                            str(message["id"]),
                            "allcall_query_call",
                            "@ALLCALL",
                            "submitted" if allcall_submitted else "blocked",
                            destination,
                        )
                delay_ms = min(
                    60_000 * (2 ** min(int(message.get("retry_count", 0)), 8)),
                    21_600_000,
                )
                database.defer_message(
                    str(message["id"]),
                    delay_ms,
                    f"no current route; discovery will retry in {delay_ms // 60000} minute(s)",
                )

    discovery_task = asyncio.create_task(discovery_loop())
    try:
        while True:
            try:
                await client.connect()
                status["connected"] = True
                database.audit("js8call.connected", {"host": args.host, "port": args.port})
                delay = 1.0

                async def handle(event: NormalizedEvent) -> None:
                    database.record_observation(event)
                    ack = parse_ack(event.value)
                    source = event.params.get("FROM")
                    if ack and isinstance(source, str):
                        kind, message_id, bitmap = ack
                        message = database.get_message(message_id)
                        if message is not None:
                            if kind == "delivered" and source.upper() == str(message["destination"]).upper():
                                metadata = parse_delivery_ack(event.value)
                                detail = "end-to-end receipt"
                                if metadata is not None:
                                    _, delivered_at_ms, path = metadata
                                    detail = f"delivered_at={delivered_at_ms}; path={'→'.join(path)}"
                                database.record_attempt(message_id, "delivery_ack", source, "received", detail)
                                if message["state"] == MessageState.IN_PROGRESS:
                                    database.transition_message(message_id, MessageState.DELIVERED)
                            else:
                                part_ack = parse_part_ack(event.value)
                                detail = bitmap or "acknowledged"
                                if part_ack is not None and part_ack.missing:
                                    detail = f"missing parts: {','.join(map(str, part_ack.missing))}"
                                    try:
                                        parts = split_human_message(message_id, str(message["body"]))
                                        for number in part_ack.missing:
                                            if number <= len(parts):
                                                await client.send_message(
                                                    f"{source} {format_human_data_part(parts[number - 1])}"
                                                )
                                        database.record_attempt(
                                            message_id, "part_resend", source, "submitted", detail
                                        )
                                    except (ValueError, RuntimeError, ConnectionError):
                                        database.record_attempt(
                                            message_id, "part_resend", source, "failed", detail
                                        )
                                database.record_attempt(message_id, "hop_ack", source, "received", detail)
                    if event.value.startswith("J8M1 D ") and isinstance(source, str) and source.upper() != status["callsign"]:
                        fields = event.value.split(" ", 4)
                        if len(fields) == 5:
                            try:
                                position, total = fields[3].split("/", 1)
                                part = MessagePart(fields[2], int(position), int(total), fields[4])
                                accumulator = reassembly.setdefault(
                                    part.message_id, MultipartAccumulator(part.message_id, part.total)
                                )
                                accumulator.add(part)
                                if accumulator.should_ack(utc_now_ms()):
                                    await client.send_message(f"{source} {format_part_ack(accumulator.receipt())}")
                                    database.audit("message.part_ack_submitted", {"message_id": part.message_id, "to": source})
                                if accumulator.receipt().complete:
                                    await client.send_message(
                                        f"{source} {format_delivery_ack(part.message_id, utc_now_ms(), (status['callsign'], source))}"
                                    )
                                    database.audit("message.delivered_ack_submitted", {"message_id": part.message_id, "to": source})
                            except (ValueError, RuntimeError, ConnectionError):
                                database.audit("message.ack_failed", {"source": source})

                reader_task = asyncio.create_task(client.read_events(handle))
                try:
                    identity = await client.request_read_only("STATION.GET_CALLSIGN")
                    status["callsign"] = identity.value.strip().upper()
                    await reader_task
                finally:
                    if not reader_task.done():
                        reader_task.cancel()
                        await asyncio.gather(reader_task, return_exceptions=True)
            except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
                status["connected"] = False
                database.audit("js8call.connection_error", {"error": type(exc).__name__})
            finally:
                status["connected"] = False
                await client.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    finally:
        discovery_task.cancel()
        await asyncio.gather(discovery_task, return_exceptions=True)
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
    parser.add_argument("--tx-mode", choices=("observe", "automatic"), default="automatic")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
