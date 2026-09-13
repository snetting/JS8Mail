"""Run the local JS8Mail mailbox and daemon."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

from js8mail.adapters.js8call.client import Js8CallClient
from js8mail.application.lifecycle import MessageState
from js8mail.application.service import MailService
from js8mail.discovery import (
    QueryScheduler,
    call_query,
    custodian_messages_query,
    messages_query,
    parse_messages_available,
    parse_query_call_response,
    retrieve_message_query,
    snr_query,
)
from js8mail.domain import NormalizedEvent, utc_now_ms
from js8mail.groups import DEFAULT_GROUPS, default_group_description, extract_groups
from js8mail.protocol import (
    CAPABILITY_TTL_MS,
    MessagePart,
    MultipartAccumulator,
    format_capability,
    format_delivery_ack,
    format_human_data_part,
    format_ordinary_message,
    format_part_ack,
    format_relay_message,
    format_store_message,
    parse_ack,
    parse_capability,
    parse_delivery_ack,
    parse_part_ack,
    split_human_message,
)
from js8mail.radio_policy import AirtimeBudget, estimate_airtime_ms
from js8mail.storage import Database

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>JS8Mail</title><style>
body{font:15px system-ui;max-width:1250px;margin:2em auto;padding:0 1em;background:#f5f7f9;color:#18222d}
 .workspace{display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,.8fr);gap:1em;align-items:stretch}.workspace section{margin:0;min-width:0}.workspace>section{min-height:260px}
section{background:white;border:1px solid #d9e0e7;border-radius:10px;padding:1em;margin:1em 0}input,textarea,select{box-sizing:border-box;width:100%;padding:.5em;margin:.25em 0 .7em}textarea{height:110px}button{background:#1769aa;color:#fff;border:0;border-radius:5px;padding:.5em .8em;margin:.2em;cursor:pointer}.danger{background:#a33}.pill{display:inline-block;padding:.3em .6em;border-radius:1em;background:#e8edf2;margin:.2em}.ok{background:#d8f3dc}.warn{background:#fff1c2}.state-pill{display:inline-block;padding:.3em .6em;border-radius:1em;margin:.2em;font-weight:600;white-space:nowrap}.state-in-progress{background:#dbeafe;color:#174ea6}.state-complete{background:#d8f3dc;color:#176b35}.state-complete-plus{background:#b7f0d0;color:#075c38}.state-failed{background:#ffd9d9;color:#8b1e1e}.state-cancelled,.state-expired{background:#e8edf2;color:#53606d}.mono{font:12px monospace;white-space:pre-wrap;overflow-wrap:anywhere}table{width:100%;table-layout:fixed}svg{display:block;max-width:100%;height:auto}td,th{text-align:left;border-bottom:1px solid #e4e9ee;padding:.5em;vertical-align:top;overflow-wrap:anywhere}details summary{cursor:pointer;padding:.25em 0}details summary::marker{color:#1769aa}
</style><h1>JS8Mail</h1><p>Offline-first mailbox · automatic RF handoff prototype</p><section><div id=status>Loading…</div></section>
<div class=workspace><section><h2>Inbox</h2><div id=inbox>Loading…</div></section>
<section><h2>Recently heard stations</h2><input id=station-search type=search placeholder='Filter callsigns or evidence'><div id=stations>Loading…</div></section>
<section><h2>Compose</h2><form id=compose>Destination<input name=destination maxlength=16 required placeholder=N0CALL>Subject<input name=subject maxlength=120>Message<textarea name=body maxlength=4096 required></textarea>Priority<select name=priority><option value=0>Normal</option><option value=1>High</option><option value=2>Urgent</option><option value=3>Emergency</option></select><button>Queue locally</button></form><span id=result></span></section>
<section><h2>Emergency groups and alerts</h2><p>Compose to an emergency group or review received broadcasts. Automatic forwarding remains opt-in.</p><div id=groups>Loading…</div><h3>Group alert inbox</h3><div id=alerts>Loading…</div></section></div>
<section><h2>Outbox</h2><div id=messages>Loading…</div></section>
<section><h2>Message route graph</h2><div id=graph-result>Select Graph on a message to inspect its evidence and attempts.</div></section>
<section><h2>Recent observations</h2><div id=observations>Loading…</div></section>
<script>
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(u,o){let r=await fetch(u,o),j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}
const confidenceName={uncertain:'No delivery evidence',discovery_in_progress:'Discovery in progress',submitted_to_js8call:'Message submitted to JS8Call',stored_at_custodian:'Stored at custodian · recipient retrieval pending',radio_acknowledged:'Radio acknowledged (hop only)',delivered_to_js8mail:'Delivered to JS8Mail client',cancelled:'Cancelled',delivery_failed:'Delivery failed',expired:'Expired'};
function statePill(x){if(x.confidence==='delivered_to_js8mail')return `<span class='state-pill state-complete-plus'>Complete+</span>`;if(x.state==='delivered')return `<span class='state-pill state-complete'>Complete</span>`;if(['failed','expired','cancelled'].includes(x.state))return `<span class='state-pill state-${esc(x.state)}'>${esc(x.state[0].toUpperCase()+x.state.slice(1))}</span>`;if(['in_progress','waiting_route','queued'].includes(x.state))return `<span class='state-pill state-in-progress'>In progress</span>`;return `<span class='state-pill'>${esc(x.state)}</span>`}
function relativeAge(seconds){seconds=Math.max(0,Number(seconds)||0);if(seconds<60)return `${Math.round(seconds)}s ago`;if(seconds<3600)return `${Math.floor(seconds/60)}m ago`;if(seconds<86400)return `${Math.floor(seconds/3600)}h ago`;return `${Math.floor(seconds/86400)}d ago`}function evidenceLabel(value){return value==='direct'?'Direct':value==='reported_target'?'Remote':value==='remote_report'?'Reported':value}
async function showGraph(id){try{let s=await api('/api/status'),g=await api('/api/graph?message_id='+encodeURIComponent(id)+'&origin='+encodeURIComponent(s.callsign||''));let cols=Math.min(4,Math.max(1,g.nodes.length)),rows=Math.max(1,Math.ceil(g.nodes.length/cols)),w=Math.max(720,cols*250+120),h=rows*120+100,nodes=g.nodes,pos={};nodes.forEach((n,i)=>pos[n]={x:60+(i%cols)*250,y:70+Math.floor(i/cols)*120});let edges=g.edges.map(e=>{let a=pos[e.from],b=pos[e.to];return `<line x1=${a.x} y1=${a.y} x2=${b.x} y2=${b.y} stroke='${e.kind==='confirmed'?'#17823b':e.kind==='attempted'?'#c77800':'#78909c'}' stroke-width=3 marker-end='url(#arrow)'/><text x=${(a.x+b.x)/2} y=${(a.y+b.y)/2-6} font-size=12>${esc(e.kind)}${e.snr!=null?' '+esc(e.snr)+'dB':''}</text>`}).join('');let circles=nodes.map(n=>`<circle cx=${pos[n].x} cy=${pos[n].y} r=28 fill='${n===g.origin?'#1769aa':n===g.destination?'#a33':'#e8edf2'}' stroke='#18222d'/><text x=${pos[n].x} y=${pos[n].y+4} text-anchor=middle font-size=12 fill='${n===g.origin||n===g.destination?'white':'#18222d'}'>${esc(n)}</text>`).join('');document.getElementById('graph-result').innerHTML=`<p><b>${esc(g.origin)} → ${esc(g.destination)}</b> · green confirmed, orange attempted, grey observed</p><svg viewBox='0 0 ${w} ${h}' width='100%' height='auto' preserveAspectRatio='xMidYMin meet' role='img' aria-label='Message route graph'><defs><marker id=arrow markerWidth=8 markerHeight=8 refX=6 refY=3 orient=auto><path d='M0,0 L0,6 L7,3 z' fill='#555'/></marker></defs>${edges}${circles}</svg>`}catch(e){document.getElementById('graph-result').textContent=e}}
function useStation(call){document.querySelector('#compose input[name=destination]').value=call;document.querySelector('#compose input[name=destination]').focus()}
let stationCache=[];function renderStations(){let q=document.getElementById('station-search').value.trim().toUpperCase();let s=stationCache.filter(x=>!q||x.callsign.includes(q)||x.evidence.join(' ').toUpperCase().includes(q));document.getElementById('stations').innerHTML=s.length?'<table><tr><th>Callsign</th><th>Age</th><th>SNR</th><th>Evidence</th><th>Action</th></tr>'+s.map(x=>`<tr><td><b>${esc(x.callsign)}</b></td><td>${esc(relativeAge(x.age_seconds))}</td><td>${x.snr==null?'—':esc(x.snr)+' dB'}</td><td>${esc(x.evidence.map(evidenceLabel).join(', '))}</td><td><button onclick="useStation('${esc(x.callsign)}')">Compose</button></td></tr>`).join('')+'</table>':'<p>No matching station evidence.</p>'}async function refreshStations(){stationCache=await api('/api/stations');renderStations()}
function renderInbox(items){document.getElementById('inbox').innerHTML=items.length?'<table><tr><th>From</th><th>Status</th><th>Message</th><th>Updated</th></tr>'+items.map(x=>`<tr><td><b>${esc(x.sender)}</b></td><td><span class='pill ${x.complete?'ok':'warn'}'>${x.complete?'Complete':'Partial · '+x.received_parts.length+'/'+x.total_parts+' parts'}</span></td><td class=mono>${esc(x.body)}</td><td>${esc(new Date(x.updated_at_ms).toLocaleString())}<br>${esc(x.path||'')}</td></tr>`).join('')+'</table>':'<p>No received messages.</p>'}
async function refresh(){let s=await api('/api/status');document.getElementById('status').innerHTML=`<span class='pill ${s.connected?'ok':'warn'}'>JS8Call: ${s.connected?'connected':'offline'}</span><span class=pill>Station: ${esc(s.callsign||'unknown')}</span><span class=pill>Speed: ${esc(s.speed??'unknown')}</span><span class=pill>TX mode: ${s.tx_mode}</span><span class=pill>Port: ${s.port}</span>`;let m=await api('/api/messages');document.getElementById('messages').innerHTML=m.length?'<table><tr><th>Message</th><th>To</th><th>Content</th><th>Action</th></tr>'+m.map(x=>`<tr><td><details ${x.state==='in_progress'?'open':''}><summary>${statePill(x)} · <span class=pill>${esc(confidenceName[x.confidence]||confidenceName.uncertain)}</span><br><small>${esc(x.id)}</small>${x.next_attempt_at_ms?` · retry ${new Date(x.next_attempt_at_ms).toLocaleTimeString()} (#${x.retry_count})`:''}</summary><div class=mono>${(x.attempts||[]).map(a=>`${esc(a.action)} → ${esc(a.target)}: ${esc(a.status)}${a.detail?' · '+esc(a.detail):''}`).join('<br>')||'No attempts recorded.'}</div></details></td><td>${esc(x.destination)}</td><td><b>${esc(x.subject||'(no subject)')}</b><br>${esc(x.body)}</td><td><button onclick="showGraph('${x.id}')">Graph</button>${['queued','waiting_route'].includes(x.state)?`<button class=danger onclick="act('${x.id}','cancel')">Cancel</button>`:''}${['failed','cancelled'].includes(x.state)?`<button onclick="act('${x.id}','retry')">Retry</button>`:''}</td></tr>`).join('')+'</table>':'<p>No messages.</p>';let o=await api('/api/observations');document.getElementById('observations').innerHTML=o.map(x=>`<div class=mono>${new Date(x.observed_at_ms).toLocaleTimeString()} ${esc(x.event_type)} ${esc(x.value)}</div>`).join('')||'<p>Waiting for JS8Call events.</p>'}
const EMERGENCY_GROUPS=['@EMCOMM','@ARES','@RACES','@RAYNET','@NTS','@SKYWARN','@WX','@AMRRON'];function useGroup(group){document.querySelector('#compose input[name=destination]').value=group;document.querySelector('#compose input[name=destination]').focus()}function renderGroups(items){let groups=items.filter(x=>EMERGENCY_GROUPS.includes(x.name));document.getElementById('groups').innerHTML=groups.length?'<table><tr><th>Group</th><th>Purpose</th><th>Seen</th><th>Action</th></tr>'+groups.map(x=>`<tr><td><b>${esc(x.name)}</b></td><td>${esc(x.description||'emergency group')}</td><td>${x.seen_count?esc(relativeAge((Date.now()-x.last_seen_at_ms)/1000)):'not yet observed'}</td><td><button onclick="useGroup('${esc(x.name)}')">Compose</button><button onclick="actGroup('${esc(x.name)}','${x.subscribed?'unsubscribe':'subscribe'}')">${x.subscribed?'Unsubscribe':'Subscribe'}</button></td></tr>`).join('')+'</table>':'<p>No emergency groups recorded.</p>'}function renderAlerts(items){let alerts=items.filter(x=>x.group_name);document.getElementById('alerts').innerHTML=alerts.length?alerts.map(x=>`<article><b>${esc(x.group_name)} · ${esc(x.sender)}</b> <span class='pill ${x.complete?'ok':'warn'}'>${x.complete?'Complete':'Partial · '+x.received_parts.length+'/'+x.total_parts}</span><div class=mono>${esc(x.body)}</div><small>${esc(new Date(x.updated_at_ms).toLocaleString())} · ${esc(x.path||'')}</small></article>`).join(''):'<p>No group alerts received.</p>'}
const refreshMailbox=refresh;refresh=async()=>{let inbox=await api('/api/inbox');renderInbox(inbox);renderAlerts(inbox);let groups=await api('/api/groups');renderGroups(groups);return refreshMailbox()};
async function actGroup(group,action){try{await api(`/api/groups/${encodeURIComponent(group)}/${action}`,{method:'POST'});refresh()}catch(e){alert(e)}}
async function act(id,a){try{await api(`/api/messages/${id}/${a}`,{method:'POST'});refresh()}catch(e){alert(e)}}
function addMessageControls(){document.querySelectorAll('#messages tr').forEach(row=>{let id=row.querySelector('small')?.textContent.trim(),state=row.querySelector('b')?.textContent.trim(),cell=row.lastElementChild;if(!id||!cell||row.dataset.controls)return;row.dataset.controls='1';if(['queued','waiting_route'].includes(state)){let b=document.createElement('button');b.textContent='Retry now';b.onclick=()=>act(id,'retry-now');cell.appendChild(b)}if(state!=='in_progress'){let b=document.createElement('button');b.textContent='Remove';b.className='danger';b.onclick=()=>act(id,'delete');cell.appendChild(b)}})}
document.getElementById('compose').onsubmit=async e=>{e.preventDefault();try{let x=await api('/api/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});document.getElementById('result').textContent='Queued '+x.id;e.target.reset();refresh()}catch(e){document.getElementById('result').textContent=e}}
document.getElementById('station-search').oninput=renderStations;
refresh().then(addMessageControls);refreshStations();setInterval(()=>{refresh().then(addMessageControls);refreshStations()},3000);
</script>"""


class Handler(BaseHTTPRequestHandler):
    service: MailService
    client: Js8CallClient
    loop: asyncio.AbstractEventLoop
    status: dict[str, Any]
    announced_destinations: set[str]
    airtime_budget: AirtimeBudget
    message_budgets: dict[str, AirtimeBudget]

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
        elif path == "/api/inbox":
            self.reply(200, self.service.database.list_inbox())
        elif path == "/api/groups":
            self.reply(200, self.service.database.list_groups())
        elif path == "/api/messages":
            self.reply(200, self.service.message_views())
        elif path == "/api/observations":
            self.reply(200, self.service.database.recent_observations(12))
        elif path == "/api/graph":
            query = parse_qs(urlparse(self.path).query)
            message_id = query.get("message_id", [""])[0]
            origin = query.get("origin", [""])[0]
            if not message_id or not origin:
                self.reply(400, {"error": "message_id and origin are required"})
            else:
                self.reply(200, self.service.message_graph(message_id, origin))
        elif path == "/api/stations":
            self.reply(200, self.service.station_views())
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
            group_parts = path.strip("/").split("/")
            if len(group_parts) == 4 and group_parts[:2] == ["api", "groups"]:
                if group_parts[3] not in {"subscribe", "unsubscribe"}:
                    raise ValueError("unknown group action")
                self.service.database.set_group_subscription(
                    unquote(group_parts[2]), group_parts[3] == "subscribe"
                )
                self.reply(200, {"ok": True})
                return
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
            elif action == "delete":
                self.service.delete(message_id)
            elif action == "retry-now":
                self.service.retry_now(message_id)
                if self.status["tx_mode"] == "automatic":
                    future = asyncio.run_coroutine_threadsafe(self.prepare(message_id), self.loop)
                    future.result(timeout=30)
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
        destination = str(message["destination"])
        announce = destination not in self.announced_destinations
        origin = str(self.status.get("callsign", "")).upper()
        plan = self.service.plan_route(origin, destination) if origin else None
        path = plan.path if plan is not None else (origin, destination)
        peer = self.service.database.peer_capabilities(destination)
        enhanced_parts = (
            split_human_message(message_id, str(message["body"]))
            if peer is not None and "MP" in peer[1]
            else ()
        )
        wire_texts: tuple[str, ...]
        if plan is not None and len(path) >= 3:
            payloads = tuple(format_human_data_part(part) for part in enhanced_parts) or (str(message["body"]),)
            wire_texts = tuple(format_relay_message(path, payload) for payload in payloads)
            action = "relay"
            target = path[1]
            detail = f"discovered path: {'→'.join(path)}"
        elif enhanced_parts:
            wire_texts = tuple(
                format_ordinary_message(destination, format_human_data_part(part))
                for part in enhanced_parts
            )
            action = "multipart"
            target = destination
            detail = f"{len(enhanced_parts)} JS8Mail parts"
        else:
            wire_texts = (format_ordinary_message(destination, str(message["body"]), announce),)
            action = "direct"
            target = destination
            detail = "initial direct attempt"
        if destination not in self.announced_destinations and self.service.database.peer_capabilities(destination) is None:
            try:
                await self.send_rf(f"{destination} {format_capability()}", message_id)
                self.service.database.record_attempt(
                    message_id, "capability", destination, "submitted", "JS8Mail capability advertisement"
                )
            except (ConnectionError, OSError, RuntimeError) as exc:
                self.service.database.record_attempt(
                    message_id, "capability", destination, "failed", type(exc).__name__
                )
        self.service.database.record_attempt(
            message_id, action, target, "started", detail
        )
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.transition_message(message_id, MessageState.IN_PROGRESS)
        try:
            for text in wire_texts:
                await self.send_rf(text, message_id)
        except (ConnectionError, OSError, RuntimeError) as exc:
            self.service.database.record_attempt(
                message_id, "direct", destination, "failed", type(exc).__name__
            )
            raise
        self.service.database.record_attempt(
            message_id, action, target, "submitted", "queued in JS8Call for next TX cycle"
        )
        self.service.database.audit(
            "message.submitted_to_js8call",
            {"message_id": message_id, "text_length": sum(len(text) for text in wire_texts), "frames": len(wire_texts)},
        )
        self.announced_destinations.add(destination)

    async def transmit_store(self, message_id: str, custodian: str) -> None:
        """Offer a legacy-compatible message to one remote custodian."""
        message = self.service.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        if message["state"] not in {MessageState.QUEUED, MessageState.WAITING_ROUTE}:
            raise ValueError("message is not ready for storage")
        destination = str(message["destination"])
        text = format_store_message(custodian, destination, str(message["body"]))
        self.service.database.record_attempt(
            message_id, "store", custodian, "started", f"offer for later retrieval by {destination}"
        )
        self.service.database.upsert_custody(message_id, custodian, "offered", "store offer submitted")
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.transition_message(message_id, MessageState.IN_PROGRESS)
        try:
            await self.send_rf(text, message_id)
        except (ConnectionError, OSError, RuntimeError) as exc:
            self.service.database.record_attempt(message_id, "store", custodian, "failed", type(exc).__name__)
            self.service.database.upsert_custody(message_id, custodian, "failed", type(exc).__name__)
            raise
        self.service.database.record_attempt(
            message_id, "store", custodian, "submitted", "queued in JS8Call for next TX cycle"
        )

    async def prepare(self, message_id: str) -> None:
        """Probe an unobserved destination before committing message airtime."""
        message = self.service.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        destination = str(message["destination"])
        if self.service.recently_answered(destination, str(self.status.get("callsign", ""))):
            await self.transmit(message_id)
            return
        probe = snr_query(destination)
        self.service.database.record_attempt(
            message_id, "snr_probe", destination, "started", "destination not recently heard"
        )
        try:
            await self.send_rf(probe, message_id)
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

    async def send_rf(self, text: str, message_id: str | None = None) -> None:
        """Reserve conservative airtime before handing a frame to JS8Call."""
        try:
            speed = int(self.status.get("speed", 1))
        except (TypeError, ValueError):
            speed = 1
        speed = max(0, min(4, speed))
        airtime_ms = estimate_airtime_ms(text, speed)
        now = utc_now_ms()
        if not self.airtime_budget.can_spend_at(airtime_ms, now):
            if message_id is not None:
                self.service.database.record_attempt(
                    message_id, "airtime_budget", "radio", "blocked",
                    f"rolling airtime budget exhausted at speed {speed}",
                )
            self.service.database.audit(
                "radio.airtime_blocked", {"message_id": message_id, "estimate_ms": airtime_ms, "speed": speed}
            )
            raise RuntimeError("local airtime budget exhausted")
        message_budget = None
        if message_id is not None:
            message_budget = self.message_budgets.setdefault(message_id, AirtimeBudget())
            if not message_budget.can_spend_at(airtime_ms, now):
                self.service.database.record_attempt(
                    message_id, "airtime_budget", "message", "blocked",
                    f"per-message airtime budget exhausted at speed {speed}",
                )
                raise RuntimeError("message airtime budget exhausted")
        await self.client.send_message(text)
        self.airtime_budget.spend_at(airtime_ms, now)
        if message_budget is not None:
            message_budget.spend_at(airtime_ms, now)
        self.service.database.audit(
            "radio.airtime_reserved", {"message_id": message_id, "estimate_ms": airtime_ms, "speed": speed}
        )


async def run(args: argparse.Namespace) -> None:
    database = Database(Path(args.database).expanduser())
    service = MailService(database)
    for group, description in DEFAULT_GROUPS:
        database.ensure_group(group, description)
    client = Js8CallClient(args.host, args.port)
    loop = asyncio.get_running_loop()
    status: dict[str, Any] = {
        "connected": False,
        "host": args.host,
        "port": args.port,
        "tx_mode": args.tx_mode,
        "callsign": "",
        "speed": "unknown",
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
            "airtime_budget": AirtimeBudget(),
            "message_budgets": {},
        },
    )
    server = ThreadingHTTPServer((args.ui_host, args.ui_port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"JS8Mail UI: http://{args.ui_host}:{args.ui_port}", flush=True)
    delay = 1.0
    query_scheduler = QueryScheduler()
    inbox_scheduler = QueryScheduler(base_delay_ms=1_800_000, max_delay_ms=21_600_000)
    custodian_scheduler = QueryScheduler(base_delay_ms=1_800_000, max_delay_ms=21_600_000)
    last_inbox_query = database.latest_audit_time(
        "discovery.query_submitted", "action", "messages_query"
    )
    if last_inbox_query is not None:
        elapsed = max(0, utc_now_ms() - last_inbox_query)
        inbox_scheduler.restore(
            "inbox:broadcast",
            int(asyncio.get_running_loop().time() * 1000),
            max(0, 1_800_000 - elapsed),
        )
    reassembly: dict[str, MultipartAccumulator] = {}
    recent_call_queries: list[tuple[int, str]] = []

    async def submit_query(
        key: str,
        text: str,
        action: str,
        target: str,
        scheduler: QueryScheduler = query_scheduler,
        route_destination: str | None = None,
    ) -> bool:
        now = int(asyncio.get_running_loop().time() * 1000)
        if not client.connected or not scheduler.due(key, now):
            return False
        try:
            await cast(Handler, handler).send_rf(text)
            database.audit(
                "discovery.query_submitted", {"action": action, "target": target, "text": text}
            )
            scheduler.record(key, now)
            if route_destination is not None:
                recent_call_queries.append((now, route_destination))
                del recent_call_queries[:-16]
            return True
        except (ConnectionError, RuntimeError):
            scheduler.record(key, now)
            return False

    async def discovery_loop() -> None:
        inbox_key = "inbox:broadcast"
        last_prune_at_ms = 0
        while True:
            await asyncio.sleep(5)
            now_wall_ms = utc_now_ms()
            if now_wall_ms - last_prune_at_ms >= 60 * 60 * 1000:
                database.prune_observations(now_ms=now_wall_ms)
                database.prune_groups(now_ms=now_wall_ms)
                last_prune_at_ms = now_wall_ms
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
            # Ask only known custodians, and only on the same restrained
            # cadence as the broadcast query.  A positive answer below is
            # followed by a targeted message-ID retrieval.
            for stored_message in database.list_messages():
                for custody in database.list_custody(str(stored_message["id"])):
                    if custody["status"] != "accepted":
                        continue
                    custodian = str(custody["custodian"])
                    key = f"custodian:{custodian}"
                    if custodian_scheduler.due(key, now):
                        submitted = await submit_query(
                            key,
                            custodian_messages_query(custodian),
                            "custodian_query_msgs",
                            custodian,
                        )
                        database.record_attempt(
                            str(stored_message["id"]),
                            "custodian_query_msgs",
                            custodian,
                            "submitted" if submitted else "blocked",
                            "checking for stored mail",
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
                if service.recently_answered(destination, str(status.get("callsign", ""))):
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
                # A query-call reply can complete a multi-hop path without
                # requiring the original destination to answer us directly.
                # Use that fresh evidence as soon as the message is due.
                if message["state"] == MessageState.WAITING_ROUTE and database.due_for_retry(str(message["id"])):
                    plan = service.plan_route(str(status.get("callsign", "")), destination)
                    if len(plan.path) >= 3:
                        database.record_attempt(
                            str(message["id"]),
                            "route",
                            destination,
                            "selected",
                            plan.explanation,
                        )
                        try:
                            await handler.transmit(cast(Handler, handler), str(message["id"]))
                        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                            database.record_attempt(
                                str(message["id"]),
                                "relay",
                                plan.path[1],
                                "deferred",
                                f"route selected but TX was unavailable: {type(exc).__name__}",
                            )
                            database.defer_message(
                                str(message["id"]), 60_000, "selected route could not be submitted"
                            )
                        continue
                promising = service.promising_stations(destination)[:3]
                if message.get("retry_count", 0) >= 3:
                    candidate_custodian = next((candidate for candidate in promising if candidate != destination), None)
                    if candidate_custodian is not None and not any(
                        item["custodian"] == candidate_custodian and item["status"] == "accepted"
                        for item in database.list_custody(str(message["id"]))
                    ):
                        try:
                            await handler.transmit_store(cast(Handler, handler), str(message["id"]), candidate_custodian)
                        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                            database.record_attempt(
                                str(message["id"]), "store", candidate_custodian, "deferred", type(exc).__name__
                            )
                            database.defer_message(str(message["id"]), 60_000, "custodian offer unavailable")
                        continue
                if (
                    message["state"] == MessageState.WAITING_ROUTE
                    and not database.due_for_retry(str(message["id"]))
                    and not promising
                ):
                    continue
                call_key = f"call-query:{destination}"
                if query_scheduler.due(call_key, now):
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
                                    route_destination=destination,
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
                            call_key,
                            call_query(destination),
                            "allcall_query_call",
                            "@ALLCALL",
                            route_destination=destination,
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
                    database.record_link_projection(event)
                    for group in extract_groups(event.value, *[str(value) for value in event.params.values()]):
                        database.observe_group(group, default_group_description(group))
                    ack = parse_ack(event.value)
                    source = event.params.get("FROM")
                    available_id = parse_messages_available(event.value)
                    if available_id is not None and isinstance(source, str):
                        for stored_message in database.list_messages(MessageState.IN_PROGRESS):
                            message_id = str(stored_message["id"])
                            if any(
                                item["custodian"].upper() == source.upper()
                                and item["status"] == "accepted"
                                for item in database.list_custody(message_id)
                            ):
                                try:
                                    await cast(Handler, handler).send_rf(retrieve_message_query(source, available_id))
                                    database.upsert_custody(
                                        message_id,
                                        source,
                                        "retrieval_pending",
                                        f"requested JS8Call message ID {available_id}",
                                    )
                                    database.record_attempt(
                                        message_id,
                                        "custodian_retrieve",
                                        source,
                                        "submitted",
                                        f"message ID {available_id}",
                                    )
                                except (ConnectionError, RuntimeError):
                                    database.record_attempt(
                                        message_id, "custodian_retrieve", source, "failed", "JS8Call unavailable"
                                    )
                    capability = parse_capability(event.value)
                    if capability is not None and isinstance(source, str):
                        version, features = capability
                        database.upsert_peer_capabilities(
                            source, version, features, utc_now_ms() + CAPABILITY_TTL_MS
                        )
                        try:
                            await cast(Handler, handler).send_rf(f"{source} {format_capability(features)}")
                            database.audit(
                                "peer.capability_ack_submitted",
                                {"peer": source.upper(), "version": version},
                            )
                        except (ConnectionError, RuntimeError):
                            database.audit("peer.capability_ack_failed", {"peer": source.upper()})
                    # Legacy JS8Call messages arrive without a JS8Mail ID.
                    # Store them too, using a deterministic local fingerprint
                    # so repeated custodian retrieval does not create copies.
                    command = event.params.get("CMD")
                    message_text = event.params.get("TEXT")
                    if (
                        isinstance(source, str)
                        and isinstance(command, str)
                        and command.strip() in {"MSG", "MSG TO:"}
                        and isinstance(message_text, str)
                        and message_text.strip()
                        and not message_text.startswith("J8M1 D ")
                    ):
                        legacy_id = "legacy-" + hashlib.sha256(
                            f"{source.upper()}\n{message_text}".encode()
                        ).hexdigest()[:16]
                        database.upsert_inbox_message(
                            source,
                            legacy_id,
                            message_text.strip(),
                            1,
                            (1,),
                            True,
                            tuple(str(event.params.get("PATH", source)).split(">")),
                            str(event.params.get("TO", "")),
                        )
                    query_response = parse_query_call_response(event.value)
                    if query_response is not None and isinstance(source, str):
                        now = utc_now_ms()
                        recent_call_queries[:] = [
                            item for item in recent_call_queries if now - item[0] <= 180_000
                        ]
                        if recent_call_queries:
                            _, queried_destination = recent_call_queries[-1]
                            snr, age_minutes = query_response
                            database.record_observation(
                                NormalizedEvent(
                                    "QUERY.CALL.RESPONSE",
                                    event.value,
                                    {
                                        "FROM": source.upper(),
                                        "TO": queried_destination,
                                        "SNR": snr,
                                        "AGE_MIN": age_minutes,
                                        "EVIDENCE": "remote_query_call_yes",
                                    },
                                    now,
                                )
                            )
                            local_call = str(status.get("callsign", "")).upper()
                            if local_call and local_call != source.upper():
                                database.record_observation(
                                    NormalizedEvent(
                                        "QUERY.CALL.REACHABILITY",
                                        event.value,
                                        {
                                            "FROM": local_call,
                                            "TO": source.upper(),
                                            "SNR": snr,
                                            "EVIDENCE": "directed_response",
                                        },
                                        now,
                                    )
                                )
                            for message in database.list_messages(MessageState.WAITING_ROUTE):
                                if str(message["destination"]).upper() == queried_destination.upper():
                                    database.record_attempt(
                                        str(message["id"]),
                                        "route_evidence",
                                        source.upper(),
                                        "received",
                                        f"heard {queried_destination} at {snr} dB, {age_minutes} minute(s) ago",
                                    )
                    if (
                        isinstance(source, str)
                        and event.event_type in {"RX.DIRECTED.ME", "RX.DIRECTED"}
                        and event.value.strip().split()[1:] == ["ACK"]
                    ):
                            for message in database.list_messages(MessageState.IN_PROGRESS):
                                if str(message["destination"]).upper() == source.upper():
                                    stored = any(
                                        item["action"] == "store"
                                        and item["target"].upper() == source.upper()
                                        and item["status"] == "submitted"
                                        for item in database.list_attempts(str(message["id"]))
                                    )
                                    if stored:
                                        database.upsert_custody(
                                            str(message["id"]), source, "accepted", "standard JS8Call store ACK"
                                        )
                                        database.record_attempt(
                                            str(message["id"]), "custody_ack", source, "received", "stored for later retrieval"
                                        )
                                        continue
                                    database.record_attempt(
                                    str(message["id"]),
                                    "standard_ack",
                                    source,
                                    "received",
                                    "standard JS8Call ACK; hop acknowledged, delivery unproven",
                                )
                    if ack and isinstance(source, str):
                        kind, message_id, bitmap = ack
                        receipt_message = database.get_message(message_id)
                        if receipt_message is not None:
                            if kind == "delivered":
                                metadata = parse_delivery_ack(event.value)
                                receipt_path = metadata[2] if metadata is not None else ()
                                destination_matches = source.upper() == str(receipt_message["destination"]).upper()
                                custody_row = next(
                                    (
                                        item for item in database.list_custody(message_id)
                                        if item["custodian"].upper() == source.upper()
                                        and item["status"] in {"accepted", "retrieval_pending", "forwarded"}
                                    ),
                                    None,
                                )
                                forwarded_matches = (
                                    custody_row is not None
                                    and str(receipt_message["destination"]).upper() in {item.upper() for item in receipt_path}
                                )
                                if destination_matches or forwarded_matches:
                                    detail = "end-to-end receipt"
                                    if metadata is not None:
                                        _, delivered_at_ms, path = metadata
                                        detail = f"delivered_at={delivered_at_ms}; path={'→'.join(path)}"
                                    database.record_attempt(message_id, "delivery_ack", source, "received", detail)
                                    if forwarded_matches:
                                        database.upsert_custody(
                                            message_id, source, "forwarded",
                                            f"custodian receipt correlated with destination {receipt_message['destination']}",
                                        )
                                        database.record_attempt(
                                            message_id, "custodian_forwarded", source, "confirmed", detail
                                        )
                                    if receipt_message["state"] == MessageState.IN_PROGRESS:
                                        database.transition_message(message_id, MessageState.DELIVERED)
                            else:
                                part_ack = parse_part_ack(event.value)
                                detail = bitmap or "acknowledged"
                                if part_ack is not None and part_ack.missing:
                                    detail = f"missing parts: {','.join(map(str, part_ack.missing))}"
                                    try:
                                        parts = split_human_message(message_id, str(receipt_message["body"]))
                                        for part in parts:
                                            database.upsert_message_part(
                                                part.message_id,
                                                part.number,
                                                part.total,
                                                part.payload,
                                                direction="outgoing",
                                                peer=source,
                                            )
                                        for number in part_ack.missing:
                                            if number <= len(parts):
                                                await cast(Handler, handler).send_rf(
                                                    f"{source} {format_human_data_part(parts[number - 1])}", message_id
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
                                if not accumulator.receipt().received:
                                    for stored_part in database.list_message_parts(
                                        part.message_id, direction="incoming", peer=source
                                    ):
                                        accumulator.add(
                                            MessagePart(
                                                part.message_id,
                                                int(stored_part["part_number"]),
                                                int(stored_part["total_parts"]),
                                                str(stored_part["payload"]),
                                            )
                                        )
                                accumulator.add(part)
                                database.upsert_message_part(
                                    part.message_id,
                                    part.number,
                                    part.total,
                                    part.payload,
                                    direction="incoming",
                                    peer=source,
                                )
                                receipt = accumulator.receipt()
                                database.upsert_inbox_message(
                                    source,
                                    part.message_id,
                                    accumulator.partial_preview(),
                                    part.total,
                                    receipt.received,
                                    receipt.complete,
                                    (source,),
                                    str(event.params.get("TO", "")),
                                )
                                if accumulator.should_ack(utc_now_ms()):
                                    await cast(Handler, handler).send_rf(f"{source} {format_part_ack(accumulator.receipt())}")
                                    database.audit("message.part_ack_submitted", {"message_id": part.message_id, "to": source})
                                if accumulator.receipt().complete:
                                    await cast(Handler, handler).send_rf(
                                        f"{source} {format_delivery_ack(part.message_id, utc_now_ms(), (status['callsign'], source))}"
                                    )
                                    database.audit("message.delivered_ack_submitted", {"message_id": part.message_id, "to": source})
                            except (ValueError, RuntimeError, ConnectionError):
                                database.audit("message.ack_failed", {"source": source})

                reader_task = asyncio.create_task(client.read_events(handle))
                try:
                    identity = await client.request_read_only("STATION.GET_CALLSIGN")
                    status["callsign"] = identity.value.strip().upper()
                    try:
                        speed = await client.request_read_only("MODE.GET_SPEED")
                        reported_speed = speed.params.get("SPEED", speed.value.strip())
                        status["speed"] = reported_speed if reported_speed != "" else "unknown"
                        database.audit("js8call.speed_detected", {"speed": status["speed"]})
                    except (ConnectionError, OSError, RuntimeError):
                        status["speed"] = "unavailable"
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
