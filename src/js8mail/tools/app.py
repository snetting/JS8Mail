"""Run the local JS8Mail mailbox and daemon."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import threading
import traceback
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from js8mail.adapters.js8call.client import Js8CallClient
from js8mail.adapters.js8call.protocol import normalize_directed_event, parse_legacy_ack
from js8mail.application.lifecycle import MessageState
from js8mail.application.service import ENHANCED_MODES, MailService
from js8mail.bands import band_from_frequency_hz, context_from_params
from js8mail.discovery import (
    PendingCallQuery,
    QueryScheduler,
    call_query,
    correlate_query_call_response,
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
    format_relay_text,
    format_store_message,
    parse_ack,
    parse_capability,
    parse_delivery_ack,
    parse_human_data_part,
    parse_part_ack,
    parse_resend_request,
    split_human_message,
)
from js8mail.radio_policy import (
    SPEED_AIRTIME_MS,
    AdaptiveSpeedPolicy,
    AirtimeBudget,
    AirtimeBudgetExceeded,
    SpeedEvidence,
    estimate_airtime_ms,
)
from js8mail.storage import Database

DIRECT_RESPONSE_DEADLINE_MS = 2 * 60 * 1000
CAPABILITY_RESPONSE_DEADLINE_MS = 45 * 1000
AUTOMATED_TX_GAP_MS = 30 * 1000
AUTOMATED_RX_WINDOW_MS = 60 * 1000
QUERY_RESPONSE_MAX_MS = 3 * 60 * 1000
LATE_QUERY_CONTEXT_MS = 15 * 60 * 1000
CAPABILITY_MAX_RESPONSE_MS = 5 * 60 * 1000
# A message may use a bounded burst, then continue in later rolling windows.
# The total is intentionally larger than the three-day default message TTL;
# the station-wide budget remains the ultimate safety ceiling.
MESSAGE_BURST_LIMIT_MS = 10 * 60 * 1000
MESSAGE_TOTAL_LIMIT_MS = 60 * 60 * 1000


def capability_response_window_ms(path: tuple[str, ...], speed: object) -> int:
    """Estimate the return-path response window after CAP arrives."""
    try:
        speed_id = int(str(speed))
    except (TypeError, ValueError):
        speed_id = 0
    cycle_ms = SPEED_AIRTIME_MS.get(speed_id, SPEED_AIRTIME_MS[0])
    hops = max(1, len(path) - 1)
    if hops == 1:
        return CAPABILITY_RESPONSE_DEADLINE_MS
    # One return slot per hop plus a modest guard. The outbound allowance is
    # added separately when the CAP is submitted, because the response timer
    # must not consume time while the CAP is still travelling outward.
    return min(CAPABILITY_MAX_RESPONSE_MS, hops * cycle_ms + 15_000)


def capability_outbound_ms(path: tuple[str, ...], text: str, speed: object) -> int:
    """Estimate the time for a CAP frame to reach the final hop."""
    try:
        speed_id = int(str(speed))
    except (TypeError, ValueError):
        speed_id = 0
    return max(1, len(path) - 1) * estimate_airtime_ms(text, speed_id)


def query_response_window_ms(action: str, speed: object) -> int:
    """Estimate how long a query may need to collect JS8Call replies."""
    try:
        speed_id = int(str(speed))
    except (TypeError, ValueError):
        speed_id = 0
    cycle_ms = SPEED_AIRTIME_MS.get(speed_id, SPEED_AIRTIME_MS[0])
    cycles = 3 if action == "allcall_query_call" else 2
    return min(QUERY_RESPONSE_MAX_MS, max(60_000, cycles * cycle_ms + 15_000))


def reverse_custody_path(
    local_call: str,
    original_sender: str,
    incoming_path: tuple[str, ...],
) -> tuple[str, ...]:
    """Build a safe reverse path when the received path identifies the origin."""
    local = local_call.strip().upper()
    original = original_sender.strip().upper()
    path = tuple(item.strip().upper() for item in incoming_path if item.strip())
    if not local or not original or local not in path:
        return ()
    local_index = path.index(local)
    forward_prefix = path[: local_index + 1]
    if not forward_prefix or forward_prefix[0] != original:
        return ()
    reverse = (local, *reversed(forward_prefix[:-1]))
    return reverse if len(reverse) >= 2 and reverse[-1] == original else ()


ROUTE_EVIDENCE_SETTLE_MAX_MS = 90_000
ROUTE_EVIDENCE_SETTLE_GUARD_MS = 10_000


def route_evidence_settling_window_ms(response_window_ms: int, speed: object) -> int:
    """Allow one or two expected RF cycles for competing query replies."""
    try:
        speed_id = int(str(speed))
    except (TypeError, ValueError):
        speed_id = 0
    cycle_ms = SPEED_AIRTIME_MS.get(speed_id, SPEED_AIRTIME_MS[0])
    expected = 2 * cycle_ms + ROUTE_EVIDENCE_SETTLE_GUARD_MS
    return min(response_window_ms, ROUTE_EVIDENCE_SETTLE_MAX_MS, max(30_000, expected))


def delivery_response_window_ms(operation: str, path: tuple[str, ...], speed: object) -> int:
    """Bound the wait for a legacy ACK after the real TX has ended.

    Direct and custodian ACKs normally arrive in the next one or two cycles.
    A relayed final ACK has to cross the reverse path, so the allowance grows
    with hop count but remains bounded.  This is deliberately a response
    deadline, not an airtime estimate; the latter is stored separately.
    """
    try:
        speed_id = int(str(speed))
    except (TypeError, ValueError):
        speed_id = 0
    cycle_ms = SPEED_AIRTIME_MS.get(speed_id, SPEED_AIRTIME_MS[0])
    hops = max(1, len(path) - 1)
    if operation == "relay":
        return min(10 * 60 * 1000, max(60_000, (hops + 1) * cycle_ms + 30_000))
    return min(3 * 60 * 1000, max(45_000, 2 * cycle_ms + 15_000))


def _recent_outbound_transaction(
    database: Database, source: str, now_ms: int
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Find one durable, unambiguous legacy transaction for an ACK.

    The parent message may already be waiting for a later route, and a late
    ACK may arrive after the first deadline.  Matching the durable transaction
    rather than message state preserves both cases.  If two live operations
    target the same responder, refuse to guess.
    """
    candidates = database.pending_transmission_for_ack(source, now_ms)
    live = [item for item in candidates if item["status"] in {"queued", "tx_active", "awaiting_ack"}]
    if len(live) > 1:
        return None
    chosen = live or candidates
    if not chosen:
        return None
    if len(chosen) > 1 and int(chosen[0]["created_at_ms"]) == int(chosen[1]["created_at_ms"]):
        return None
    transaction = max(chosen, key=lambda item: int(item["created_at_ms"]))
    message = database.get_message(str(transaction["message_id"]))
    if message is None:
        return None
    return message, transaction

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>JS8Mail</title><style>
body{font:15px system-ui;max-width:1250px;margin:2em auto;padding:0 1em;background:#f5f7f9;color:#18222d}.topbar{position:sticky;top:0;z-index:10;background:#f5f7f9;padding:.35em 0 .5em}
 .workspace{display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,.8fr);gap:1em;align-items:stretch}.workspace section{margin:0;min-width:0}.workspace>section{min-height:260px}.inbox-panel{max-height:360px;overflow:auto}.live-panel{min-height:300px}.live-panel svg{width:100%;min-height:280px;background:#fbfcfd;border-radius:6px}.stations-panel{grid-column:2;grid-row:2 / span 2}.groups-panel{grid-column:1}#messages th:nth-child(2),#messages td:nth-child(2){width:8em}#messages th:nth-child(4),#messages td:nth-child(4){width:17em;white-space:nowrap}@media(max-width:800px){.workspace{display:block}.workspace>section{margin:1em 0}.stations-panel{grid-column:auto;grid-row:auto}#messages th:nth-child(4),#messages td:nth-child(4){width:auto;white-space:normal}}
section{background:white;border:1px solid #d9e0e7;border-radius:10px;padding:1em;margin:1em 0}input,textarea,select{box-sizing:border-box;width:100%;padding:.5em;margin:.25em 0 .7em}textarea{height:110px}button{background:#1769aa;color:#fff;border:0;border-radius:5px;padding:.5em .8em;margin:.2em;cursor:pointer}.danger{background:#a33}.pill{display:inline-block;padding:.3em .6em;border-radius:1em;background:#e8edf2;margin:.2em}.ok{background:#d8f3dc}.warn{background:#fff1c2}.state-pill{display:inline-block;padding:.3em .6em;border-radius:1em;margin:.2em;font-weight:600;white-space:nowrap}.state-in-progress{background:#dbeafe;color:#174ea6}.state-complete{background:#d8f3dc;color:#176b35}.state-complete-plus{background:#b7f0d0;color:#075c38}.state-failed{background:#ffd9d9;color:#8b1e1e}.state-cancelled,.state-expired{background:#e8edf2;color:#53606d}#status .pill:nth-child(3){display:none}.mono{font:12px monospace;white-space:pre-wrap;overflow-wrap:anywhere}table{width:100%;table-layout:fixed}svg{display:block;max-width:100%;height:auto}td,th{text-align:left;border-bottom:1px solid #e4e9ee;padding:.5em;vertical-align:top;overflow-wrap:anywhere}#messages th:nth-child(4),#messages td:nth-child(4){width:18em;white-space:normal}.outbox-actions{display:flex;flex-wrap:wrap;gap:.3em;align-items:flex-start}.outbox-actions button{margin:0;padding:.4em .55em;white-space:nowrap}details summary{cursor:pointer;padding:.25em 0}details summary::marker{color:#1769aa}
</style><div class=topbar><h1>JS8Mail</h1><p>Resilient radio mail for reliable offline comms · by <a href='https://www.oh3spn.fi' target=_blank rel=noopener>OH3SPN</a> <button onclick="useStation('OH3SPN')">Compose to OH3SPN</button></p><section><div id=status>Loading…</div><div id=radio-leds class=leds><span id=led-rx class='led on-rx'>RX</span><span id=led-dcd class='led'>DCD</span><span id=led-tx class='led'>TX</span><span id=led-err class='led'>ERR</span><span id=led-js8 class='led'>JS8</span></div></section></div><style>.leds{display:inline-flex;gap:.3em;margin-left:.5em;vertical-align:middle}.led{padding:.25em .5em;border-radius:1em;background:#e8edf2;color:#53606d;font-size:12px;font-weight:600}.led.on-rx{background:#d8f3dc;color:#176b35}.led.on-tx{background:#ffd9d9;color:#8b1e1e}.led.on-dcd{background:#fff1c2;color:#785500}.led.on-err{background:#8b1e1e;color:white}#status .pill:nth-child(3){display:none}</style>
<section class=system-panel><h2>System sending mode</h2><p><label>Default JS8M sending mode <select name=enhanced_mode id=default-enhanced-mode title='Default for new messages'><option value=opportunistic>Opportunistic (recommended)</option><option value=standard>Standard JS8Call</option><option value=required>Required JS8M</option></select></label></p><small>This is the default for new directed messages. Standard uses ordinary JS8Call immediately; Opportunistic uses JS8M for known capable stations and otherwise uses ordinary mail; Required waits for a JS8M capability response. Group broadcasts always use Standard.</small></section>
<div class=workspace><section class=compose-panel><h2>Compose</h2><form id=compose>Destination<input name=destination maxlength=16 required placeholder=N0CALL>Subject<input name=subject maxlength=120>Message<textarea name=body maxlength=4096 required></textarea>Priority<select name=priority><option value=0>Normal</option><option value=1>High</option><option value=2>Urgent</option><option value=3>Emergency</option></select>Message mode<select name=enhanced_mode title='Override the default for this message'><option value=''>Use system default</option><option value=standard>Standard JS8Call</option><option value=opportunistic>Opportunistic</option><option value=required>Required JS8M</option></select><button>Queue locally</button></form><span id=result></span></section>
<section class=live-panel><h2>Live RF Activity <small id=live-graph-meta></small></h2><div id=live-graph><p>Waiting for active-band observations.</p></div></section>
<section class=inbox-panel><h2>Inbox</h2><div id=inbox>Loading…</div></section>
<section class=stations-panel><h2>Recently heard stations</h2><input id=station-search type=search placeholder='Filter callsigns or evidence'><div id=stations>Loading…</div></section>
<section class=groups-panel><h2>Emergency groups and alerts</h2><p>Compose to an emergency group or review received broadcasts. Automatic forwarding remains opt-in.</p><div id=groups>Loading…</div><h3>Group alert inbox</h3><div id=alerts>Loading…</div></section></div>
<section><h2>Outbox</h2><div id=control-events></div><div id=messages>Loading…</div></section>
<section><h2>Message route graph</h2><div id=graph-result>Select Graph on a message to inspect its evidence and attempts.</div></section>
<section><h2>Recent observations</h2><div id=observations>Loading…</div></section>
<script>
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(u,o){let r=await fetch(u,o),j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}
const confidenceName={new:'New · waiting for discovery',uncertain:'No delivery evidence',discovery_in_progress:'Discovery in progress',submitted_to_js8call:'Queued in JS8Call · awaiting TX',awaiting_delivery_ack:'TX submitted · awaiting delivery ACK',awaiting_custodian_ack:'Store offer submitted · awaiting custodian ACK',delivery_uncertain:'Delivery unconfirmed · retry pending',stored_at_custodian:'Delivered to custodian',radio_acknowledged:'Radio acknowledged (hop only)',delivered_to_js8mail:'Delivered to JS8Mail client',cancelled:'Cancelled',delivery_failed:'Delivery failed',expired:'Expired'};
function statePill(x){if(x.tx_active)return `<span class='state-pill state-failed'>In progress · TX</span>`;if(x.confidence==='delivered_to_js8mail')return `<span class='state-pill state-complete-plus'>Complete+</span>`;if(x.state==='delivered')return `<span class='state-pill state-complete'>Complete</span>`;if(x.state==='stored')return `<span class='state-pill state-complete'>Stored</span>`;if(['failed','expired','cancelled'].includes(x.state))return `<span class='state-pill state-${esc(x.state)}'>${esc(x.state[0].toUpperCase()+x.state.slice(1))}</span>`;if(x.state==='queued'&&!(x.attempts||[]).length)return `<span class='state-pill state-in-progress'>New</span>`;if(['in_progress','waiting_route','queued'].includes(x.state))return `<span class='state-pill state-in-progress'>In progress</span>`;return `<span class='state-pill'>${esc(x.state)}</span>`}
function relativeAge(seconds){seconds=Math.max(0,Number(seconds)||0);if(seconds<60)return `${Math.round(seconds)}s ago`;if(seconds<600)return `${Math.floor(seconds/60)}m ${Math.floor(seconds%60)}s ago`;if(seconds<3600)return `${Math.floor(seconds/60)}m ago`;if(seconds<86400)return `${Math.floor(seconds/3600)}h ago`;return `${Math.floor(seconds/86400)}d ago`}function evidenceLabel(value){return value==='direct'?'Direct':value==='reported_target'?'Remote':value==='remote_report'?'Reported':value}
async function showGraph(id){try{let s=await api('/api/status'),g=await api('/api/graph?message_id='+encodeURIComponent(id)+'&origin='+encodeURIComponent(s.callsign||''));let cols=Math.min(4,Math.max(1,g.nodes.length)),rows=Math.max(1,Math.ceil(g.nodes.length/cols)),w=Math.max(720,cols*250+120),h=rows*120+100,nodes=g.nodes,pos={};nodes.forEach((n,i)=>pos[n]={x:60+(i%cols)*250,y:70+Math.floor(i/cols)*120});let edges=g.edges.map(e=>{let a=pos[e.from],b=pos[e.to];return `<line x1=${a.x} y1=${a.y} x2=${b.x} y2=${b.y} stroke='${e.kind==='confirmed'?'#17823b':e.kind==='attempted'?'#c77800':'#78909c'}' stroke-width=3 marker-end='url(#arrow)'/><text x=${(a.x+b.x)/2} y=${(a.y+b.y)/2-6} font-size=12>${esc(e.kind)}${e.snr!=null?' '+esc(e.snr)+'dB':''}</text>`}).join('');let circles=nodes.map(n=>`<circle cx=${pos[n].x} cy=${pos[n].y} r=28 fill='${n===g.origin?'#1769aa':n===g.destination?'#a33':'#e8edf2'}' stroke='#18222d'/><text x=${pos[n].x} y=${pos[n].y+4} text-anchor=middle font-size=12 fill='${n===g.origin||n===g.destination?'white':'#18222d'}'>${esc(n)}</text>`).join('');document.getElementById('graph-result').innerHTML=`<p><b>${esc(g.origin)} → ${esc(g.destination)}</b> · green confirmed, orange attempted, grey observed</p><svg viewBox='0 0 ${w} ${h}' width='100%' height='auto' preserveAspectRatio='xMidYMin meet' role='img' aria-label='Message route graph'><defs><marker id=arrow markerWidth=8 markerHeight=8 refX=6 refY=3 orient=auto><path d='M0,0 L0,6 L7,3 z' fill='#555'/></marker></defs>${edges}${circles}</svg>`}catch(e){document.getElementById('graph-result').textContent=e}}
const baseShowGraph=showGraph;showGraph=async id=>{await baseShowGraph(id);let svg=document.querySelector('#graph-result svg');if(!svg)return;svg.querySelectorAll('line').forEach((line,index)=>{let x1=Number(line.getAttribute('x1')),y1=Number(line.getAttribute('y1')),x2=Number(line.getAttribute('x2')),y2=Number(line.getAttribute('y2'));if(!Number.isFinite(x1)||!Number.isFinite(y1)||!Number.isFinite(x2)||!Number.isFinite(y2))return;let bend=(index%2?1:-1)*Math.min(28,Math.max(10,Math.hypot(x2-x1,y2-y1)/10)),mx=(x1+x2)/2,my=(y1+y2)/2,curve=document.createElementNS('http://www.w3.org/2000/svg','path');curve.setAttribute('d',`M${x1} ${y1} Q${mx-bend} ${my+bend} ${x2} ${y2}`);curve.setAttribute('stroke',line.getAttribute('stroke')||'#78909c');curve.setAttribute('stroke-width',line.getAttribute('stroke-width')||'3');curve.setAttribute('marker-end','url(#arrow)');curve.setAttribute('fill','none');line.replaceWith(curve)})};
// Straight edges plus explicit endpoint highlighting are easier to read than
// curved edges when several stations share a route graph.
showGraph=async id=>{await baseShowGraph(id);try{let s=await api('/api/status'),g=await api('/api/graph?message_id='+encodeURIComponent(id)+'&origin='+encodeURIComponent(s.callsign||'')),circles=[...document.querySelectorAll('#graph-result svg circle')];circles.forEach((circle,index)=>{let node=g.nodes[index],edges=g.edges.filter(e=>e.from===node||e.to===node),kind=edges.some(e=>e.kind==='confirmed')?'confirmed':edges.some(e=>e.kind==='attempted')?'attempted':'observed',color=kind==='confirmed'?'#17823b':kind==='attempted'?'#c77800':'#78909c';circle.setAttribute('stroke',color);circle.setAttribute('stroke-width',kind==='observed'?'2':'5');let title=document.createElementNS('http://www.w3.org/2000/svg','title');title.textContent=kind==='confirmed'?'Confirmed route endpoint':kind==='attempted'?'Attempted route endpoint':'Observed RF node';circle.appendChild(title)})}catch(e){}};
function curveLiveGraphEdges(svg){return}
function markLiveGraphNodes(){let svg=document.querySelector('#live-graph svg');if(!svg)return;let links=[...svg.querySelectorAll('line')];svg.querySelectorAll('circle').forEach(circle=>{let x=Number(circle.getAttribute('cx')),y=Number(circle.getAttribute('cy')),kinds=links.filter(line=>[ ['x1','y1'],['x2','y2'] ].some(([px,py])=>Number(line.getAttribute(px))===x&&Number(line.getAttribute(py))===y)).map(line=>line.getAttribute('stroke')),kind=kinds.includes('#00a83b')?'confirmed':kinds.includes('#c77800')?'attempted':'observed',colors={confirmed:['#17823b','#d8f3dc'],attempted:['#c77800','#fff1c2'],observed:['#78909c','#e8edf2']},color=colors[kind];circle.setAttribute('stroke',color[0]);circle.setAttribute('stroke-width',kind==='observed'?'2':'4');circle.setAttribute('fill',color[1])})}new MutationObserver(markLiveGraphNodes).observe(document.getElementById('live-graph'),{childList:true,subtree:true});
function useStation(call){document.querySelector('#compose input[name=destination]').value=call;document.querySelector('#compose input[name=destination]').focus();document.querySelector('.compose-panel')?.scrollIntoView({behavior:'smooth',block:'start'})}
let stationCache=[];function renderStations(){let q=document.getElementById('station-search').value.trim().toUpperCase();let s=stationCache.filter(x=>!q||x.callsign.includes(q)||x.evidence.join(' ').toUpperCase().includes(q));document.getElementById('stations').innerHTML=s.length?'<table style="table-layout:fixed;width:100%"><colgroup><col style="width:18%"><col style="width:18%"><col style="width:14%"><col style="width:18%"><col style="width:18%"><col style="width:14%"></colgroup><tr><th style="white-space:nowrap">Callsign</th><th style="white-space:nowrap">Age</th><th style="white-space:nowrap">SNR</th><th>Path</th><th style="white-space:nowrap">Action</th><th title="JS8Mail capability" style="white-space:nowrap;text-align:center">JS8M</th></tr>'+s.map(x=>`<tr><td style="white-space:nowrap"><b>${esc(x.callsign)}</b></td><td style="white-space:nowrap">${esc(relativeAge(x.age_seconds))}</td><td style="white-space:nowrap">${x.snr==null?'—':esc(x.snr)+' dB'}</td><td style="white-space:normal">${x.evidence.map(evidenceLabel).map(esc).join('<br>')}</td><td style="white-space:nowrap"><button style="white-space:nowrap" onclick="useStation('${esc(x.callsign)}')">Compose</button></td><td title="${x.js8m?'JS8Mail capable':'Not identified as JS8Mail capable'}" style="white-space:nowrap;text-align:center;padding-left:.5em;padding-right:.5em;color:#16a34a;font-size:1.15em">${x.js8m?'●':''}</td></tr>`).join('')+'</table>':'<p>No matching station evidence.</p>'}async function refreshStations(){stationCache=await api('/api/stations');renderStations()}
function renderInbox(items){document.getElementById('inbox').innerHTML=items.length?'<table><tr><th>From</th><th>Status</th><th>Message</th><th>Updated</th></tr>'+items.map(x=>`<tr><td><b>${esc(x.sender)}</b></td><td><span class='pill ${x.complete?'ok':'warn'}'>${x.complete?'Complete':'Partial · '+x.received_parts.length+'/'+x.total_parts+' parts'}</span></td><td class=mono>${esc(x.body)}</td><td>${esc(new Date(x.updated_at_ms).toLocaleString())}<br>${esc(x.path||'')}</td></tr>`).join('')+'</table>':'<p>No received messages.</p>'}
function renderControlEvents(items){let el=document.getElementById('control-events');if(!el)return;el.innerHTML=items.length?`<details class='control-events'><summary>Automatic delivery confirmations (${items.length})</summary><div class=mono>${items.map(x=>`<div><span class='timeline-time'>${new Date(x.created_at_ms).toLocaleTimeString()}</span> <b>${esc(x.label||'Automatic delivery update')}</b> → ${esc(x.target||'unknown')}: <span class='pill ${x.status==='submitted'?'ok':x.status==='failed'?'bad':'warn'}'>${esc(x.status||'waiting')}</span><br>${esc(x.detail||'')}${x.path?`<br>Path: ${esc(x.path)}`:''}</div>`).join('')}</div></details>`:''}
async function refreshControlEvents(){try{renderControlEvents(await api('/api/control-events'))}catch(_error){}}
refreshControlEvents();setInterval(refreshControlEvents,3000);
async function refresh(){let s=await api('/api/status'),statusHtml=`<span class='pill ${s.connected?'ok':'warn'}'>JS8Call: ${s.connected?'connected':'offline'}</span><span class=pill>Station: ${esc(s.callsign||'unknown')}</span><span class='pill ${s.paused?'warn':'ok'}'>RF: ${s.paused?'paused':'active'}</span><span class=pill>Port: ${s.port}</span><button onclick="togglePause()">${s.paused?'Resume RF':'Pause RF'}</button>`;let defaultMode=document.getElementById('default-enhanced-mode');if(defaultMode&&defaultMode.value!==s.enhanced_mode)defaultMode.value=s.enhanced_mode||'opportunistic';let statusEl=document.getElementById('status');if(statusEl.dataset.rendered!==statusHtml){statusEl.innerHTML=statusHtml;statusEl.dataset.rendered=statusHtml}let m=await api('/api/messages');let openIds=[...document.querySelectorAll('#messages details[open]')].map(d=>d.dataset.id);document.getElementById('messages').innerHTML=m.length?'<table><tr><th>Message</th><th>To</th><th>Content</th><th>Action</th></tr>'+m.map(x=>`<tr><td><details data-id='${esc(x.id)}' ${openIds.includes(x.id)?'open':''}><summary>${statePill(x)} · <span class=pill>${esc(confidenceName[x.confidence]||confidenceName.uncertain)}</span><br><small>${esc(x.id)}</small>${x.next_attempt_at_ms?` · retry ${new Date(x.next_attempt_at_ms).toLocaleTimeString()} (#${x.retry_count})`:''}</summary><div class=mono>${(x.attempts||[]).map(a=>`<span class=timeline-time>${new Date(a.created_at_ms).toLocaleTimeString()}</span> ${esc(a.action)} → ${esc(a.target)}: ${esc(a.status)}${a.detail?' · '+esc(a.detail):''}`).join('<br>')||'No attempts recorded.'}</div></details></td><td>${esc(x.destination)}</td><td><b>${esc(x.subject||'(no subject)')}</b><br>${esc(x.body)}</td><td><button onclick="showGraph('${x.id}')">Graph</button>${['queued','waiting_route','in_progress'].includes(x.state)?`<button onclick="act('${x.id}','retry-now')">Retry now</button>`:''}${['queued','waiting_route','in_progress'].includes(x.state)?`<button class=danger onclick="act('${x.id}','cancel')">Cancel</button>`:''}${['failed','cancelled','expired','delivered'].includes(x.state)?`<button class=danger onclick="act('${x.id}','delete')">Remove</button>`:''}</td></tr>`).join('')+'</table>':'<p>No messages.</p>';let o=await api('/api/observations');document.getElementById('observations').innerHTML=o.map(x=>`<div class=mono>${new Date(x.observed_at_ms).toLocaleTimeString()} ${esc(x.event_type)} ${esc(x.value)}</div>`).join('')||'<p>Waiting for JS8Call events.</p>'}
const EMERGENCY_GROUPS=['@EMCOMM','@ARES','@RACES','@RAYNET','@NTS','@SKYWARN','@WX','@AMRRON'];function useGroup(group){document.querySelector('#compose input[name=destination]').value=group;document.querySelector('#compose input[name=destination]').focus()}function renderGroups(items){let groups=items.filter(x=>EMERGENCY_GROUPS.includes(x.name));document.getElementById('groups').innerHTML=groups.length?'<table><tr><th>Group</th><th>Purpose</th><th>Seen</th><th>Action</th></tr>'+groups.map(x=>`<tr><td><b>${esc(x.name)}</b></td><td>${esc(x.description||'emergency group')}</td><td>${x.seen_count?esc(relativeAge((Date.now()-x.last_seen_at_ms)/1000)):'not yet observed'}</td><td><button onclick="useGroup('${esc(x.name)}')">Compose</button><button onclick="actGroup('${esc(x.name)}','${x.subscribed?'unsubscribe':'subscribe'}')">${x.subscribed?'Unsubscribe':'Subscribe'}</button></td></tr>`).join('')+'</table>':'<p>No emergency groups recorded.</p>'}function renderAlerts(items){let alerts=items.filter(x=>x.group_name);document.getElementById('alerts').innerHTML=alerts.length?alerts.map(x=>`<article><b>${esc(x.group_name)} · ${esc(x.sender)}</b> <span class='pill ${x.complete?'ok':'warn'}'>${x.complete?'Complete':'Partial · '+x.received_parts.length+'/'+x.total_parts}</span><div class=mono>${esc(x.body)}</div><small>${esc(new Date(x.updated_at_ms).toLocaleString())} · ${esc(x.path||'')}</small></article>`).join(''):'<p>No group alerts received.</p>'}
const refreshMailbox=refresh;refresh=async()=>{let inbox=await api('/api/inbox');renderInbox(inbox);renderAlerts(inbox);let groups=await api('/api/groups');renderGroups(groups);return refreshMailbox()};const updateRadioLeds=async()=>{let s=await api('/api/status'),activity=s.connected?(s.radio_activity||'RX'):'ERR';document.querySelectorAll('#radio-leds .led').forEach(x=>x.className='led');let led=document.getElementById('led-'+activity.toLowerCase());if(led)led.className='led on-'+activity.toLowerCase()};const refreshWithRadioState=refresh;refresh=async()=>{await refreshWithRadioState();await updateRadioLeds()};
async function actGroup(group,action){try{await api(`/api/groups/${encodeURIComponent(group)}/${action}`,{method:'POST'});refresh()}catch(e){alert(e)}}
async function deleteInboxMessage(item){if(!item||!confirm('Delete this local inbox message?'))return;try{await api(`/api/inbox/${encodeURIComponent(item.sender)}/${encodeURIComponent(item.message_id)}/delete`,{method:'POST'});refresh()}catch(e){alert(e)}}
async function act(id,a){try{await api(`/api/messages/${id}/${a}`,{method:'POST'});refresh()}catch(e){alert(e)}}
async function togglePause(){try{let s=await api('/api/status');await api(`/api/control/${s.paused?'resume':'pause'}`,{method:'POST'});refresh()}catch(e){alert(e)}}
const expandedMessages=new Set;document.addEventListener('click',e=>{let summary=e.target.closest?.('#messages details summary');if(!summary)return;setTimeout(()=>{let detail=summary.parentElement,id=detail?.querySelector('small')?.textContent.trim();if(id){if(detail.open)expandedMessages.add(id);else expandedMessages.delete(id)}},0)});function restoreExpanded(){document.querySelectorAll('#messages details').forEach(d=>{let id=d.querySelector('small')?.textContent.trim();if(id&&expandedMessages.has(id))d.open=true})}const refreshKeepExpanded=refresh;refresh=async()=>{if(document.querySelector('#messages details[open]')){await updateRadioLeds();return}await refreshKeepExpanded();restoreExpanded()};
const refreshStable=async()=>{let inbox=await api('/api/inbox');renderInbox(inbox);renderAlerts(inbox);let groups=await api('/api/groups');renderGroups(groups);await refreshMailbox()};refresh=async()=>{await refreshStable();await updateRadioLeds();restoreExpanded()};async function addMessageControls(){if(!document.getElementById('outbox-layout-style')){let style=document.createElement('style');style.id='outbox-layout-style';style.textContent='#messages th:nth-child(3),#messages td:nth-child(3){width:15em}#messages th:nth-child(4),#messages td:nth-child(4){width:21em;white-space:normal}#messages td:nth-child(4) button{margin:0;padding:.4em .55em;white-space:nowrap}@media(max-width:800px){#messages th:nth-child(3),#messages td:nth-child(3),#messages th:nth-child(4),#messages td:nth-child(4){width:auto}}';document.head.appendChild(style)}document.querySelectorAll('#messages tr').forEach(row=>{let content=row.cells?.[2],details=row.querySelector('details');if(!content||content.dataset.preview)return;let full=content.innerHTML;content.dataset.preview='true';content.innerHTML=`<div class=message-preview>${full}</div>`;if(details){let expanded=document.createElement('div');expanded.className='message-full outbox-full-content';expanded.innerHTML=`<b>Full content</b><br>${full}`;details.appendChild(expanded)}})}
document.getElementById('compose').onsubmit=async e=>{e.preventDefault();try{let x=await api('/api/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});document.getElementById('result').textContent='Queued '+x.id;e.target.reset();refresh()}catch(e){document.getElementById('result').textContent=e}}
document.getElementById('default-enhanced-mode').onchange=async e=>{try{await api('/api/settings/enhanced-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:e.target.value})});document.getElementById('result').textContent='Default JS8M mode saved'}catch(err){document.getElementById('result').textContent=err}}
document.addEventListener('submit',e=>{if(e.target.id==='compose')setTimeout(()=>document.getElementById('messages')?.closest('section')?.scrollIntoView({behavior:'smooth',block:'start'}),700)},true);
document.getElementById('station-search').oninput=renderStations;
const destinationInput=document.querySelector('#compose input[name="destination"]');
const enhancedModeInput=document.querySelector('#compose select[name="enhanced_mode"]');
const destinationCapability=document.createElement('small');
destinationCapability.id='destination-capability';
destinationCapability.style.display='block';
destinationInput?.after(destinationCapability);
let capabilityLookup=0;
async function updateDestinationCapability(){
  let callsign=destinationInput?.value.trim().toUpperCase()||'';
  let lookup=++capabilityLookup;
  destinationCapability.textContent='';
  if(!callsign||callsign.startsWith('@'))return;
  try{
    let result=await api('/api/capability?callsign='+encodeURIComponent(callsign));
    if(lookup!==capabilityLookup||!result.js8m)return;
    destinationCapability.innerHTML=' <span style="color:#16a34a;font-weight:600">● JS8M capable</span> · Opportunistic mode recommended';
    if(enhancedModeInput?.value!=='opportunistic'){
      let button=document.createElement('button');
      button.type='button'; button.textContent='Use Opportunistic';
      button.onclick=()=>{enhancedModeInput.value='opportunistic';updateDestinationCapability()};
      destinationCapability.append(' ',button);
    }
  }catch(_error){if(lookup===capabilityLookup)destinationCapability.textContent=''}
}
destinationInput?.addEventListener('input',updateDestinationCapability);
enhancedModeInput?.addEventListener('change',updateDestinationCapability);
function showInboxMessage(item){let modal=document.getElementById('message-modal');if(!modal){modal=document.createElement('div');modal.id='message-modal';modal.innerHTML='<div class="modal-card" role="dialog" aria-modal="true"><button class="danger modal-close" onclick="closeInboxMessage()">Close</button><div id="message-modal-content"></div></div>';document.body.appendChild(modal)}let delivery={direct:'Direct',forwarded:'Forwarded',stored_collected:'Stored → collected',group_broadcast:'Group broadcast'}[item.delivery]||'Direct';document.getElementById('message-modal-content').innerHTML=`<h2>${esc(item.subject||'(no subject)')}</h2><p><b>From:</b> ${esc(item.sender)} · <b>Status:</b> ${item.complete?'Complete':'Partial · '+item.received_parts.length+'/'+item.total_parts+' parts'} · <b>Protocol:</b> ${item.protocol==='js8m'?'JS8Mail':'Standard'}</p><p><b>Delivery:</b> ${esc(delivery)}<br><b>Path:</b> ${esc(item.path||item.sender||'Unknown')}</p><div class=message-full>${esc(item.body)}</div><p><small>Received ${esc(new Date(item.updated_at_ms).toLocaleString())}</small></p><button onclick='replyToInboxMessage(inboxItems[${window.inboxItems?.indexOf(item)??-1}])'>Reply</button>`;modal.style.display='flex'}function closeInboxMessage(){let modal=document.getElementById('message-modal');if(modal)modal.style.display='none'}function replyToInboxMessage(item){if(!item)return;closeInboxMessage();let destination=document.querySelector('#compose input[name=destination]'),subject=document.querySelector('#compose input[name=subject]'),body=document.querySelector('#compose textarea[name=body]');destination.value=item.sender;subject.value=item.subject?('Re: '+item.subject).slice(0,120):'';body.focus();document.querySelector('.compose-panel')?.scrollIntoView({behavior:'smooth',block:'start'})}
const inboxRender=renderInbox;renderInbox=items=>{document.getElementById('inbox').innerHTML=items.length?'<table><tr><th>From</th><th>Status</th><th>Message</th><th>Action</th></tr>'+items.map((x,i)=>`<tr><td><b>${esc(x.sender)}</b></td><td><span class='pill ${x.complete?'ok':'warn'}'>${x.complete?'Complete':'Partial · '+x.received_parts.length+'/'+x.total_parts}</span><br><span class='pill ${x.protocol==='js8m'?'enhanced':''}'>${x.protocol==='js8m'?'JS8Mail':'Standard'}</span></td><td><div class=message-preview>${esc(x.body)}</div></td><td><button onclick='showInboxMessage(inboxItems[${i}])'>Open</button><button class=danger onclick='deleteInboxMessage(inboxItems[${i}])'>Delete</button></td></tr>`).join('')+'</table>':'<p>No received messages.</p>';window.inboxItems=items};
let modalStyle=document.createElement('style');modalStyle.textContent='#message-modal{display:none;position:fixed;inset:0;background:#18222d88;z-index:20;align-items:center;justify-content:center;padding:1em}.modal-card{background:white;border-radius:10px;box-shadow:0 8px 30px #18222d66;max-width:720px;width:min(720px,100%);max-height:85vh;overflow:auto;padding:1.2em}.modal-close{float:right}.message-preview{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;white-space:pre-wrap}.message-full{white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid #d9e0e7;border-radius:6px;padding:1em;background:#f7f9fb}.timeline-time{color:#64748b;font-variant-numeric:tabular-nums}.pill.good,.pill.enhanced{background:#b7f0d0;color:#075c38}.pill.bad{background:#ffd9d9;color:#8b1e1e}';document.head.appendChild(modalStyle);
let outboxPreviewStyle=document.createElement('style');outboxPreviewStyle.textContent='#messages td:nth-child(3){height:5.5em;max-height:5.5em;overflow:hidden;line-height:1.25}#messages td:nth-child(3) .message-preview{max-height:4.5em}.confidence-stored_at_custodian,.confidence-delivered_to_js8mail{background:#b7f0d0;color:#075c38}.confidence-awaiting_delivery_ack,.confidence-awaiting_custodian_ack{background:#fff1c2;color:#7a4b00}.confidence-delivery_uncertain{background:#ffd9d9;color:#8b1e1e}.confidence-radio_acknowledged{background:#dbeafe;color:#174ea6}';document.head.appendChild(outboxPreviewStyle);
function styleOutboxConfidence(){document.querySelectorAll('#messages details summary .pill').forEach(p=>{let text=p.textContent||'',key=text.startsWith('Delivered to custodian')?'stored_at_custodian':text.startsWith('Delivered to JS8Mail')?'delivered_to_js8mail':text.startsWith('Store offer submitted')?'awaiting_custodian_ack':text.startsWith('TX submitted')?'awaiting_delivery_ack':text.startsWith('Delivery unconfirmed')?'delivery_uncertain':text.startsWith('Radio acknowledged')?'radio_acknowledged':'';if(key)p.classList.add('confidence-'+key)})}new MutationObserver(styleOutboxConfidence).observe(document.getElementById('messages'),{childList:true,subtree:true});
function renderLiveGraph(g){let box=document.getElementById('live-graph');if(!box)return;if(!g.nodes.length){box.innerHTML='<p>Waiting for active-band observations.</p>';return}let cols=Math.min(6,Math.max(2,Math.ceil(Math.sqrt(g.nodes.length)))),rows=Math.ceil(g.nodes.length/cols),w=Math.max(720,cols*150+80),h=Math.max(280,rows*90+70),pos={};g.nodes.forEach((n,i)=>pos[n]={x:50+(i%cols)*150,y:45+Math.floor(i/cols)*90});let edges=g.edges.map(e=>{let a=pos[e.from],b=pos[e.to],color=e.kind==='reciprocal'?'#00a83b':e.kind==='active_one_way'?'#c77800':'#78909c',opacity=e.kind==='reciprocal'?Math.max(.55,e.freshness):e.freshness,width=(e.js8m?5:2)+3*e.freshness;return `<line x1=${a.x} y1=${a.y} x2=${b.x} y2=${b.y} stroke='${color}' stroke-opacity='${opacity}' stroke-width='${width}'/>`}).join('');let circles=g.nodes.map(n=>`<g><circle cx=${pos[n].x} cy=${pos[n].y} r=25 fill='#e8edf2' stroke='#18222d'/><text x=${pos[n].x} y=${pos[n].y+4} text-anchor=middle font-size=11>${esc(n)}</text></g>`).join('');box.innerHTML=`<p class=mono>Band ${esc(g.band)} · ${g.nodes.length} stations · ${g.edges.length} links · last 2 hours</p><svg viewBox='0 0 ${w} ${h}' preserveAspectRatio='xMidYMin meet' role='img' aria-label='Live radio activity graph'>${edges}${circles}</svg><small>Green reciprocal · orange active one-way · grey isolated one-way · thick links carry JS8Mail</small>`}async function refreshLiveGraph(){try{let g=await api('/api/live-graph');window.liveGraphData=g;renderLiveGraph(g)}catch(e){document.getElementById('live-graph').textContent='Live graph unavailable'}}refreshLiveGraph();setInterval(refreshLiveGraph,3000);
function formatDial(hz){let n=Number(hz);return Number.isFinite(n)&&n>0?(n/1000000).toFixed(5)+' MHz':'not set'}
function applyLiveEdgeSemantics(){let g=window.liveGraphData,svg=document.querySelector('#live-graph svg');if(!g||!svg)return;let edges=[...svg.querySelectorAll('line, path')].filter(e=>e.tagName.toLowerCase()==='line'||!e.closest('defs'));edges.slice(0,g.edges.length).forEach((element,index)=>{let edge=g.edges[index],palette=edge.kind==='reciprocal'?['#16a34a','1',4]:edge.kind==='active_one_way'?['#f59e0b','1',3]:['#cbd5e1','.75',2],color=palette[0],opacity=palette[1],width=edge.js8m?5:palette[2];element.setAttribute('data-edge-kind',String(edge.kind));element.setAttribute('stroke',color);element.setAttribute('stroke-opacity',opacity);element.setAttribute('opacity','1');element.setAttribute('stroke-width',String(width))})}new MutationObserver(()=>applyLiveEdgeSemantics()).observe(document.getElementById('live-graph'),{childList:true,subtree:true});
function strengthenLiveGraphColors(){let svg=document.querySelector('#live-graph svg');if(!svg)return;svg.querySelectorAll('line').forEach(line=>{let stroke=line.getAttribute('stroke'),opacity=Number(line.getAttribute('stroke-opacity')||1);if(stroke==='#00a83b'){line.setAttribute('stroke','#00c853');line.setAttribute('stroke-opacity',String(Math.max(.75,opacity)))}else if(stroke==='#c77800'){line.setAttribute('stroke','#ff6d00');line.setAttribute('stroke-opacity',String(Math.max(.7,opacity)))}})}new MutationObserver(strengthenLiveGraphColors).observe(document.getElementById('live-graph'),{childList:true,subtree:true});
let liveLegendBreakObserver=new MutationObserver(()=>document.querySelectorAll('#live-graph small').forEach(s=>{if(s.innerHTML.includes(' · thick links carry JS8Mail'))s.innerHTML=s.innerHTML.replace(' · thick links carry JS8Mail','<br>Thick links carry JS8Mail')}));liveLegendBreakObserver.observe(document.getElementById('live-graph'),{childList:true,subtree:true});
let liveLegendObserver=new MutationObserver(()=>{let live=document.getElementById('live-graph');if(live&&live.innerHTML.includes('grey isolated one-way'))live.innerHTML=live.innerHTML.replace('grey isolated one-way','grey aged one-way')});liveLegendObserver.observe(document.getElementById('live-graph'),{childList:true,subtree:true});
function curveLiveGraphEdges(svg){if(!svg)return;svg.querySelectorAll('line').forEach((line,index)=>{let x1=Number(line.getAttribute('x1')),y1=Number(line.getAttribute('y1')),x2=Number(line.getAttribute('x2')),y2=Number(line.getAttribute('y2'));if(!Number.isFinite(x1)||!Number.isFinite(y1)||!Number.isFinite(x2)||!Number.isFinite(y2))return;let bend=(index%2?1:-1)*Math.min(20,Math.max(8,Math.hypot(x2-x1,y2-y1)/12)),mx=(x1+x2)/2,my=(y1+y2)/2,curve=document.createElementNS('http://www.w3.org/2000/svg','path');curve.setAttribute('d',`M${x1} ${y1} Q${mx-bend} ${my+bend} ${x2} ${y2}`);curve.setAttribute('stroke',line.getAttribute('stroke')||'#78909c');curve.setAttribute('stroke-opacity',line.getAttribute('stroke-opacity')||'1');curve.setAttribute('stroke-width',line.getAttribute('stroke-width')||'2');curve.setAttribute('fill','none');line.replaceWith(curve)})}let liveGraphObserver=new MutationObserver(()=>curveLiveGraphEdges(document.querySelector('#live-graph svg')));liveGraphObserver.observe(document.getElementById('live-graph'),{childList:true,subtree:true});
document.addEventListener('click',e=>{let button=e.target.closest?.('#messages button');if(button&&button.textContent.trim()==='Graph')setTimeout(()=>document.querySelector('#graph-result')?.closest('section')?.scrollIntoView({behavior:'smooth',block:'start'}),50)});
let actionGapStyle=document.createElement('style');actionGapStyle.textContent='#messages td:nth-child(4) button{margin-right:.45em!important;margin-bottom:.25em!important}#messages td:nth-child(4) button:last-child{margin-right:0!important}section{scroll-margin-top:8rem}';document.head.appendChild(actionGapStyle);
function styleStatusPills(){let bar=document.getElementById('status'),p=bar?.querySelectorAll('.pill');if(!p||p.length<5)return;let s=window.lastStatus||{};p[0].className='pill '+(s.connected?'good':'bad');p[1].className='pill '+(s.callsign?'good':'bad');p[2].className='pill '+(s.speed!==''&&s.speed!=='unknown'&&s.speed!=='unavailable'?'good':'bad');p[3].className='pill '+(s.tx_mode?'good':'bad');p[4].className='pill '+(s.connected?'good':'bad')}
async function updateBandStatus(){try{let s=await api('/api/status'),el=document.getElementById('status-band');window.lastStatus=s;if(!el){el=document.createElement('span');el.id='status-band';el.className='pill';document.getElementById('status').appendChild(el)}let valid=Boolean(s.band)&&Number(s.dial_frequency)>0;el.className='pill '+(valid?'good':'bad');let value=`Band: ${s.band||'not set'} · Dial: ${formatDial(s.dial_frequency)}`;if(el.textContent!==value)el.textContent=value;styleStatusPills()}catch(e){}}
document.head.insertAdjacentHTML('beforeend',"<style>#status .pill:nth-child(3){display:inline-block!important}</style>");updateBandStatus();setInterval(updateBandStatus,3000);
// refresh() redraws #status, so keep the band pill attached to the current
// status contents instead of allowing that redraw to remove it.
new MutationObserver(()=>updateBandStatus()).observe(document.getElementById('status'),{childList:true});
function movePauseControl(){let bar=document.getElementById('status'),band=document.getElementById('status-band'),button=bar?.querySelector('button');if(bar&&band&&button&&band.nextElementSibling!==button)band.after(button)}new MutationObserver(movePauseControl).observe(document.getElementById('status'),{childList:true});setInterval(movePauseControl,3000);movePauseControl();
async function updateProtocolLeds(){try{let s=await api('/api/status'),now=Date.now(),dcd=document.getElementById('led-dcd'),bar=document.getElementById('radio-leds'),js8=document.getElementById('led-js8');if(dcd)dcd.className='led'+(Number(s.dcd_until_ms||0)>now?' on-dcd':'');if(!js8&&bar){js8=document.createElement('span');js8.id='led-js8';js8.className='led';js8.textContent='JS8';bar.appendChild(js8)}if(js8)js8.className='led'+(Number(s.js8_activity_until_ms||0)>now?' on-js8':'')}catch(e){}}
let protocolLedStyle=document.createElement('style');protocolLedStyle.textContent='.led.on-js8{background:#d9d2ff;color:#4b2c82}';document.head.appendChild(protocolLedStyle);updateProtocolLeds();setInterval(updateProtocolLeds,250);
const refreshWithoutOpenOutbox=async()=>{let inbox=await api('/api/inbox');renderInbox(inbox);renderAlerts(inbox);let groups=await api('/api/groups');renderGroups(groups);if(!document.querySelector('#messages details[open],#messages .outbox-full-content'))await refreshMailbox();await updateRadioLeds();restoreExpanded()};refresh=refreshWithoutOpenOutbox;
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
    tx_lock: asyncio.Lock
    last_tx_at_ms: int | None
    next_tx_not_before_ms: int | None
    active_transaction_id: int | None
    auto_speed: bool

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self.service.database.close_thread_connection()

    async def _maybe_adapt_speed(self, peer: str) -> None:
        """Apply the conservative per-peer speed policy before a payload."""
        try:
            current = int(self.status.get("speed", ""))
        except (TypeError, ValueError):
            return
        if current not in SPEED_AIRTIME_MS:
            return
        raw = self.service.database.speed_evidence(
            str(self.status.get("callsign", "")),
            peer,
            str(self.status.get("band", "")),
        )
        evidence = {
            speed: SpeedEvidence(
                successes=int(values.get("successes", 0)),
                failures=int(values.get("failures", 0)),
                average_snr=(
                    float(values["average_snr"])
                    if isinstance(values.get("average_snr"), (int, float))
                    else None
                ),
            )
            for speed, values in raw.items()
        }
        decision = AdaptiveSpeedPolicy().recommend(current, evidence)
        self.status["speed_recommendation"] = {
            "peer": peer,
            "speed": decision.speed,
            "changed": decision.changed,
            "explanation": decision.explanation,
        }
        self.service.database.audit(
            "radio.speed_recommendation",
            {"peer": peer, "speed": decision.speed, "changed": decision.changed},
        )
        if self.auto_speed and decision.changed:
            try:
                await self.client.set_speed(decision.speed)
            except (ConnectionError, OSError, RuntimeError):
                self.service.database.audit(
                    "radio.speed_change_unavailable", {"peer": peer, "speed": decision.speed}
                )
            else:
                self.status["speed"] = decision.speed
                self.service.database.audit(
                    "radio.speed_changed", {"peer": peer, "speed": decision.speed}
                )

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
            active_id = self.status.get("tx_message_id")
            views = self.service.message_views()
            for view in views:
                view["tx_active"] = bool(active_id and view.get("id") == active_id)
            self.reply(200, views)
        elif path == "/api/control-events":
            self.reply(200, self.service.database.recent_control_events())
        elif path == "/api/observations":
            self.reply(200, self.service.database.recent_observations(12))
        elif path == "/api/graph":
            query = parse_qs(urlparse(self.path).query)
            message_id = query.get("message_id", [""])[0]
            origin = query.get("origin", [""])[0]
            band = query.get("band", [""])[0] or str(self.status.get("band", ""))
            if not message_id or not origin:
                self.reply(400, {"error": "message_id and origin are required"})
            else:
                self.reply(200, self.service.message_graph(message_id, origin, band=band))
        elif path == "/api/live-graph":
            query = parse_qs(urlparse(self.path).query)
            band = query.get("band", [""])[0] or str(self.status.get("band", ""))
            self.reply(200, self.service.live_activity_graph(band))
        elif path == "/api/stations":
            band = parse_qs(urlparse(self.path).query).get("band", [""])[0] or str(self.status.get("band", ""))
            self.reply(200, self.service.station_views(band=band))
        elif path == "/api/capability":
            callsign = parse_qs(urlparse(self.path).query).get("callsign", [""])[0]
            if not callsign.strip():
                self.reply(400, {"error": "callsign is required"})
            else:
                self.reply(200, {"callsign": callsign.strip().upper(), "js8m": self.service.is_js8m_capable(callsign)})
        elif path == "/api/route":
            query = parse_qs(urlparse(self.path).query)
            origin = query.get("origin", [""])[0]
            destination = query.get("destination", [""])[0]
            band = query.get("band", [""])[0] or str(self.status.get("band", ""))
            if not origin or not destination:
                self.reply(400, {"error": "origin and destination are required"})
            else:
                self.reply(200, asdict(self.service.plan_route(origin, destination, band=band)))
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
            if path == "/api/control/pause":
                self.status["paused"] = True
                self.service.database.audit("radio.automation_paused", {"source": "ui"})
                if self.client.connected:
                    future = asyncio.run_coroutine_threadsafe(self.client.halt(), self.loop)
                    try:
                        future.result(timeout=5)
                    except (ConnectionError, OSError, RuntimeError, TimeoutError):
                        # The local pause is still authoritative when the
                        # installed JS8Call build does not expose TX.HALT.
                        self.service.database.audit(
                            "radio.halt_unavailable", {"source": "ui"}
                        )
                self.reply(200, {"ok": True, "paused": True})
                return
            if path == "/api/control/resume":
                self.status["paused"] = False
                self.service.database.audit("radio.automation_resumed", {"source": "ui"})
                self.reply(200, {"ok": True, "paused": False})
                return
            if path == "/api/settings/enhanced-mode":
                mode = str(payload.get("mode", "")).strip().lower()
                if mode not in ENHANCED_MODES:
                    raise ValueError("mode must be standard, opportunistic, or required")
                self.status["enhanced_mode"] = mode
                self.service.database.set_configuration("enhanced_mode", mode)
                self.service.database.audit("settings.enhanced_mode_changed", {"mode": mode})
                self.reply(200, {"ok": True, "enhanced_mode": mode})
                return
            if path == "/api/messages":
                requested_mode = str(payload.get("enhanced_mode", "")).strip().lower()
                if not requested_mode:
                    requested_mode = str(self.status.get("enhanced_mode", "opportunistic"))
                message_id = self.service.compose(
                    str(payload.get("destination", "")),
                    str(payload.get("subject", "")),
                    str(payload.get("body", "")),
                    int(payload.get("priority", 0)),
                    requested_mode,
                )
                # Let the scheduler place the first probe behind any active
                # delivery, capability negotiation, or discovery exchange.
                # This preserves a listening opportunity for the existing
                # transaction instead of allowing a newly composed message to
                # fill the next TX slot immediately.
                self.reply(201, {"id": message_id})
                return
            if len(group_parts) == 5 and group_parts[:2] == ["api", "inbox"]:
                if group_parts[4] != "delete":
                    raise ValueError("unknown inbox action")
                self.service.database.delete_inbox_message(
                    unquote(group_parts[2]), unquote(group_parts[3])
                )
                self.reply(200, {"ok": True})
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

    async def transmit(self, message_id: str, selected_plan: Any | None = None) -> None:
        if self.status.get("paused"):
            raise RuntimeError("RF automation is paused")
        if self.status.get("tx_mode") != "automatic":
            raise RuntimeError("automatic RF transmission is disabled")
        message = self.service.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        if message["state"] not in {MessageState.QUEUED, MessageState.WAITING_ROUTE}:
            raise ValueError("message is not ready to send")
        destination = str(message["destination"])
        announce = destination not in self.announced_destinations
        origin = str(self.status.get("callsign", "")).upper()
        # A newly queued message always gets one direct delivery attempt after
        # the short reachability probe.  A graph route may be better for a
        # later attempt, but using it for the first payload would make an
        # operator-entered destination unexpectedly relay-first.
        delivery_actions = {"direct", "multipart", "relay", "store"}
        first_delivery_attempt = not any(
            attempt["action"] in delivery_actions
            and attempt["status"] in {"started", "submitted", "failed"}
            for attempt in self.service.database.list_attempts(message_id)
        )
        # The scheduler may already have selected a route based on a fresh
        # reply. Never recompute it here: doing so used to turn an indirect
        # selection back into a direct first payload attempt.
        plan = selected_plan
        if plan is None and origin and not first_delivery_attempt:
            plan = self.service.plan_route(
                origin,
                destination,
                attempted_paths=self.service.database.attempted_message_paths(message_id),
                band=str(self.status.get("band", "")),
            )
        if plan is not None and getattr(plan, "action", None) == "defer":
            raise RuntimeError("no usable route selected")
        path = plan.path if plan is not None else (origin, destination)
        enhanced_mode = str(
            message.get("enhanced_mode") or self.status.get("enhanced_mode", "opportunistic")
        ).lower()
        peer = (
            self.service.database.peer_capabilities(destination)
            if enhanced_mode != "standard"
            else None
        )
        capability_attempts = [
            attempt for attempt in self.service.database.list_attempts(message_id)
            if attempt["action"] == "capability" and attempt["status"] == "submitted"
        ]
        capability_window_ms = capability_response_window_ms(
            path, self.status.get("speed", 0)
        )
        if (
            enhanced_mode == "required"
            and not destination.startswith("@")
            and peer is None
            and capability_attempts
        ):
            capability_sent_at = int(capability_attempts[-1]["created_at_ms"])
            recorded_window = re.search(
                r"response window (\d+)s", str(capability_attempts[-1].get("detail", ""))
            )
            if recorded_window is not None:
                capability_window_ms = int(recorded_window.group(1)) * 1000
            elapsed = utc_now_ms() - capability_sent_at
            if elapsed < capability_window_ms:
                self.service.database.record_attempt(
                    message_id, "capability_wait", destination, "waiting",
                    "waiting for JS8Mail capability response",
                )
                self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
                self.service.database.defer_message(
                    message_id,
                    max(5_000, capability_window_ms - elapsed),
                    "waiting for capability response before ordinary fallback",
                )
                return
            self.service.database.record_attempt(
                message_id, "capability_timeout", destination, "fallback",
                "no capability response; using ordinary JS8Call delivery",
            )
        enhanced_parts = (
            split_human_message(message_id, str(message["body"]))
            if peer is not None and "MP" in peer[1]
            else ()
        )
        wire_texts: tuple[str, ...]
        if plan is not None and len(path) >= 3:
            payloads = tuple(
                format_human_data_part(part, origin, destination) for part in enhanced_parts
            ) or (str(message["body"]),)
            wire_texts = tuple(format_relay_message(path, payload) for payload in payloads)
            action = "relay"
            target = path[1]
            detail = f"discovered path: {'→'.join(path)}"
        elif enhanced_parts:
            wire_texts = tuple(
                format_ordinary_message(
                    destination, format_human_data_part(part, origin, destination)
                )
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
        if origin and len(path) >= 2:
            self.service.database.record_message_path(message_id, path)
        # Advertise before the first payload so a JS8Mail peer can recognize
        # and prepare for enhanced framing. This is opportunistic: no response
        # is awaited, and ordinary stations remain valid recipients.
        if (
            enhanced_mode == "required"
            and
            not destination.startswith("@")
            and destination not in self.announced_destinations
            and self.service.database.peer_capabilities(destination) is None
        ):
            try:
                capability_text = (
                    f"{destination} {format_capability()}"
                    if len(path) < 3
                    else format_relay_text(path, format_capability())
                )
                capability_window_ms = min(
                    CAPABILITY_MAX_RESPONSE_MS,
                    capability_outbound_ms(path, capability_text, self.status.get("speed", 0))
                    + capability_window_ms,
                )
                await Handler.send_rf(self, capability_text, message_id)
                self.service.database.record_attempt(
                    message_id,
                    "capability",
                    destination,
                    "submitted",
                    f"JS8Mail capability advertisement; awaiting response for "
                    f"{capability_window_ms // 1000}s",
                )
                self.announced_destinations.add(destination)
                self.service.database.record_attempt(
                    message_id, "capability_wait", destination, "waiting",
                    f"waiting for JS8Mail capability response; estimated window "
                    f"{capability_window_ms // 1000}s for {max(1, len(path) - 1)} hop(s)",
                )
                self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
                self.service.database.defer_message(
                    message_id,
                    capability_window_ms,
                    f"waiting for capability response before ordinary fallback "
                    f"({capability_window_ms // 1000}s estimated)",
                )
                return
            except AirtimeBudgetExceeded as exc:
                detail = (
                    f"{exc.scope} airtime budget exhausted; radio is idle and policy blocked TX"
                )
                self.service.database.record_attempt(
                    message_id, "capability", destination,
                    "failed" if exc.scope == "per-message-total" else "deferred", detail
                )
                if exc.scope == "per-message-total":
                    self.service.database.transition_message(message_id, MessageState.FAILED)
                else:
                    self.service.database.defer_message(
                        message_id, 15 * 60 * 1000, detail, increment_retry=False
                    )
                return
            except (ConnectionError, OSError, RuntimeError) as exc:
                self.service.database.record_attempt(
                    message_id,
                    "capability",
                    destination,
                    "deferred",
                    f"JS8Call unavailable or busy: {type(exc).__name__}",
                )
                self.service.database.defer_message(
                    message_id,
                    30_000,
                    "CAP handoff deferred until JS8Call is available; retrying in 30 seconds",
                    increment_retry=False,
                )
                return
        self.service.database.record_attempt(
            message_id, action, target, "started", detail
        )
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.transition_message(message_id, MessageState.IN_PROGRESS)
        try:
            speed = int(self.status.get("speed", 0))
        except (TypeError, ValueError):
            speed = 0
        transaction_id = self.service.database.begin_transmission_transaction(
            message_id,
            action,
            target,
            destination,
            tuple(path),
            hashlib.sha256("\n".join(wire_texts).encode("utf-8")).hexdigest(),
            sum(estimate_airtime_ms(text, speed if speed in SPEED_AIRTIME_MS else 0) for text in wire_texts),
            delivery_response_window_ms(action, tuple(path), speed),
            int(message.get("retry_count", 0)),
        )
        self.active_transaction_id = transaction_id
        try:
            if target and not target.startswith("@"):
                await self._maybe_adapt_speed(target)
            for part in enhanced_parts:
                self.service.database.upsert_message_part(
                    part.message_id, part.number, part.total, part.payload,
                    direction="outgoing", peer=destination,
                )
            for text in wire_texts:
                await Handler.send_rf(self, text, message_id)
        except (ConnectionError, OSError, RuntimeError) as exc:
            self.service.database.mark_transmission_unconfirmed(transaction_id)
            if self.active_transaction_id == transaction_id:
                self.active_transaction_id = None
            self.service.database.record_attempt(message_id, action, target, "failed", type(exc).__name__)
            raise
        except Exception as exc:
            self.service.database.mark_transmission_unconfirmed(transaction_id)
            if self.active_transaction_id == transaction_id:
                self.active_transaction_id = None
            self.service.database.record_attempt(
                message_id,
                action,
                target,
                "failed",
                f"local error {type(exc).__name__}: {str(exc)[:240]}",
            )
            self.service.database.audit(
                "message.transmit_unexpected_error",
                {
                    "message_id": message_id,
                    "action": action,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:240],
                    "traceback": traceback.format_exc(limit=8)[-2000:],
                },
            )
            raise
        self.service.database.mark_transmission_submitted(transaction_id)
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
        enhanced_mode = str(
            message.get("enhanced_mode") or self.status.get("enhanced_mode", "opportunistic")
        ).lower()
        # CAP is sent before the first enhanced/ordinary payload. Do not let
        # the custodian fallback bypass that negotiation on the next scheduler
        # pass while the CAP frame is still in flight or awaiting a response.
        if (
            enhanced_mode == "required"
            and not destination.startswith("@")
            and self.service.database.peer_capabilities(destination) is None
        ):
            capability_attempts = [
                attempt for attempt in self.service.database.list_attempts(message_id)
                if attempt["action"] == "capability" and attempt["status"] == "submitted"
            ]
            if capability_attempts:
                elapsed = utc_now_ms() - int(capability_attempts[-1]["created_at_ms"])
                if elapsed < CAPABILITY_RESPONSE_DEADLINE_MS:
                    remaining = max(5_000, CAPABILITY_RESPONSE_DEADLINE_MS - elapsed)
                    self.service.database.record_attempt(
                        message_id, "capability_wait", destination, "waiting",
                        "waiting for JS8Mail capability response before store fallback",
                    )
                    self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
                    self.service.database.defer_message(
                        message_id, remaining,
                        "waiting for capability response before store fallback",
                    )
                    return
        text = format_store_message(custodian, destination, str(message["body"]))
        origin = str(self.status.get("callsign", "")).upper()
        if origin:
            self.service.database.record_message_path(message_id, (origin, custodian.upper()))
        self.service.database.record_attempt(
            message_id, "store", custodian, "started", f"offer for later retrieval by {destination}"
        )
        self.service.database.upsert_custody(message_id, custodian, "offered", "store offer submitted")
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.transition_message(message_id, MessageState.IN_PROGRESS)
        try:
            speed = int(self.status.get("speed", 0))
        except (TypeError, ValueError):
            speed = 0
        transaction_id = self.service.database.begin_transmission_transaction(
            message_id,
            "store",
            custodian,
            custodian,
            (origin, custodian.upper()) if origin else (custodian.upper(),),
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            estimate_airtime_ms(text, speed if speed in SPEED_AIRTIME_MS else 0),
            delivery_response_window_ms("store", (origin, custodian.upper()) if origin else (custodian.upper(),), speed),
            int(message.get("retry_count", 0)),
        )
        self.active_transaction_id = transaction_id
        try:
            await self._maybe_adapt_speed(custodian)
            await Handler.send_rf(self, text, message_id)
        except (ConnectionError, OSError, RuntimeError) as exc:
            self.service.database.mark_transmission_unconfirmed(transaction_id)
            if self.active_transaction_id == transaction_id:
                self.active_transaction_id = None
            self.service.database.record_attempt(message_id, "store", custodian, "failed", type(exc).__name__)
            self.service.database.upsert_custody(message_id, custodian, "failed", type(exc).__name__)
            raise
        except Exception as exc:
            self.service.database.mark_transmission_unconfirmed(transaction_id)
            if self.active_transaction_id == transaction_id:
                self.active_transaction_id = None
            self.service.database.record_attempt(message_id, "store", custodian, "failed", type(exc).__name__)
            self.service.database.upsert_custody(message_id, custodian, "failed", type(exc).__name__)
            raise
        self.service.database.mark_transmission_submitted(transaction_id)
        self.service.database.record_attempt(
            message_id, "store", custodian, "submitted", "queued in JS8Call for next TX cycle"
        )

    async def prepare(self, message_id: str) -> None:
        """Probe before committing payload airtime, then use normal discovery."""
        message = self.service.database.get_message(message_id)
        if message is None:
            raise KeyError(message_id)
        destination = str(message["destination"])
        if destination.startswith("@"):
            # Group traffic is explicitly operator-addressed and must not be
            # preceded by a group-wide SNR? probe or capability fan-out.
            await self.transmit(message_id)
            return
        # Even when stale direct or indirect evidence exists, the first action
        # for a newly queued destination is the small direct SNR probe.  This
        # prevents spending a long JS8Call frame on a station that is not
        # currently reachable.  A response causes discovery_loop to submit
        # the first payload directly; a timeout falls through to route and
        # custodian discovery.
        probe = snr_query(destination)
        self.service.database.record_attempt(
            message_id, "snr_probe", destination, "started", "destination not recently heard"
        )
        probe_busy = False
        try:
            await Handler.send_rf(self, probe, message_id)
        except (ConnectionError, OSError, RuntimeError) as exc:
            probe_busy = True
            self.service.database.record_attempt(
                message_id,
                "snr_probe",
                destination,
                "deferred",
                f"JS8Call unavailable or busy: {type(exc).__name__}",
            )
        else:
            self.service.database.record_attempt(
                message_id, "snr_probe", destination, "submitted", "waiting for RF evidence"
            )
        self.service.database.transition_message(message_id, MessageState.WAITING_ROUTE)
        self.service.database.defer_message(
            message_id,
            30_000 if probe_busy else 60_000,
            "JS8Call busy or unavailable; retrying probe in 30 seconds"
            if probe_busy
            else "listening for probe response; discovery fallback in 1 minute(s)",
            increment_retry=not probe_busy,
        )

    def log_message(self, format: str, *args: object) -> None:
        return

    async def send_rf(self, text: str, message_id: str | None = None) -> None:
        """Reserve conservative airtime before handing a frame to JS8Call."""
        async with self.tx_lock:
            await Handler._send_rf_serialized(self, text, message_id)

    async def _send_rf_serialized(self, text: str, message_id: str | None = None) -> None:
        """Submit one frame only after the prior TX and RX hold have cleared."""
        if self.status.get("paused"):
            raise RuntimeError("RF automation is paused")
        if self.status.get("tx_mode") != "automatic":
            raise RuntimeError("automatic RF transmission is disabled")
        # If JS8Call exposes the live PTT state, never queue behind an active
        # transmission. The timeout is deliberately bounded so a broken or
        # stale status event cannot deadlock the daemon forever.
        if self.status.get("radio_activity") == "TX":
            deadline = asyncio.get_running_loop().time() + 180
            while self.status.get("radio_activity") == "TX" and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.25)
        now = utc_now_ms()
        not_before = self.next_tx_not_before_ms or 0
        if self.last_tx_at_ms is not None:
            not_before = max(not_before, self.last_tx_at_ms + AUTOMATED_TX_GAP_MS)
        if not_before > now:
            wait_ms = not_before - now
            if wait_ms > 0:
                self.service.database.audit(
                    "radio.tx_pacing_wait",
                    {"message_id": message_id, "wait_ms": wait_ms},
                )
                await asyncio.sleep(wait_ms / 1000)
                now = utc_now_ms()
        try:
            speed = int(self.status.get("speed", 0))
        except (TypeError, ValueError):
            speed = 1
        if speed not in SPEED_AIRTIME_MS:
            speed = 0
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
            raise AirtimeBudgetExceeded("rolling")
        message_budget = None
        if message_id is not None:
            message_budget = self.message_budgets.setdefault(
                message_id,
                AirtimeBudget(
                    window_limit_ms=MESSAGE_BURST_LIMIT_MS,
                    message_limit_ms=MESSAGE_TOTAL_LIMIT_MS,
                ),
            )
            saved_message_airtime = self.service.database.message_airtime_used(message_id)
            if saved_message_airtime and message_budget.message_used_ms == 0:
                message_budget.message_used_ms = saved_message_airtime
            if not message_budget.can_spend_at(airtime_ms, now):
                self.service.database.record_attempt(
                    message_id, "airtime_budget", "message", "blocked",
                    f"per-message airtime budget exhausted at speed {speed}",
                )
                scope = (
                    "per-message-total"
                    if message_budget.message_used_ms + airtime_ms > message_budget.message_limit_ms
                    else "per-message-window"
                )
                raise AirtimeBudgetExceeded(scope)
        # A short, independent protocol LED makes API/RF handoff visible even
        # when the radio remains in its normal RX state.
        # Reserve before handing text to JS8Call. If the daemon dies after
        # submission but before the next line, the durable counters are still
        # conservative rather than silently under-counting airtime. A rejected
        # API submission may over-count slightly, which is safer than a retry
        # storm or duty-cycle breach.
        if not self.airtime_budget.spend_at(airtime_ms, now):
            raise AirtimeBudgetExceeded("rolling")
        if message_budget is not None and not message_budget.spend_at(airtime_ms, now):
            raise AirtimeBudgetExceeded("per-message")
        if message_budget is not None and message_id is not None:
            self.service.database.save_message_airtime(message_id, message_budget.message_used_ms)
        self.service.database.save_airtime_state(
            self.airtime_budget.window_started_at_ms,
            self.airtime_budget.window_used_ms,
            self.airtime_budget.message_used_ms,
        )
        self.status["js8_activity_until_ms"] = utc_now_ms() + 1_000
        if message_id is not None:
            self.status["tx_message_id"] = message_id
        await self.client.send_message(text)
        self.last_tx_at_ms = utc_now_ms()
        # The API submission is not the end of RF transmission. Hold the next
        # automated submission past the conservative airtime estimate and a
        # receive window for ACKs/replies. A later RIG.PTT TX->RX event can
        # extend this hold from the actual end of transmission.
        self.next_tx_not_before_ms = self.last_tx_at_ms + airtime_ms + AUTOMATED_RX_WINDOW_MS
        self.service.database.audit(
            "radio.airtime_reserved", {"message_id": message_id, "estimate_ms": airtime_ms, "speed": speed}
        )


async def run(args: argparse.Namespace) -> None:
    database = Database(Path(args.database).expanduser().resolve())
    service = MailService(database)
    for group, description in DEFAULT_GROUPS:
        database.ensure_group(group, description)
    configured_mode = database.get_configuration("enhanced_mode", "opportunistic")
    if configured_mode not in ENHANCED_MODES:
        configured_mode = "opportunistic"
    client = Js8CallClient(args.host, args.port)
    loop = asyncio.get_running_loop()
    saved_airtime = database.airtime_state()
    saved_window_start = saved_airtime.get("window_started_at_ms")
    airtime_budget = AirtimeBudget(
        # The radio-wide budget is governed by its rolling duty-cycle
        # window.  The five-minute ceiling is intentionally reserved for
        # each individual message budget below; applying it here would
        # eventually block the entire station after a few unrelated tests.
        message_limit_ms=15 * 60 * 1000,
        window_used_ms=int(saved_airtime.get("window_used_ms") or 0),
        window_started_at_ms=int(saved_window_start) if saved_window_start is not None else None,
    )
    status: dict[str, Any] = {
        "connected": False,
        "host": args.host,
        "port": args.port,
        "tx_mode": args.tx_mode,
        "paused": False,
        "callsign": "",
        "band": "",
        "dial_frequency": None,
        "speed": "unknown",
        "enhanced_mode": configured_mode,
        "radio_activity": "RX",
        "next_tx_not_before_ms": 0,
        "dcd_until_ms": 0,
        "js8_activity_until_ms": 0,
        "tx_message_id": None,
        "active_transaction_id": None,
        "speed_recommendation": None,
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
            "airtime_budget": airtime_budget,
            "message_budgets": {},
            "tx_lock": asyncio.Lock(),
            "last_tx_at_ms": None,
            "auto_speed": args.auto_speed,
        },
    )
    # The HTTP server creates request-handler instances, but the background
    # scheduler also needs a bound Handler object. Calling methods through the
    # dynamic class itself loses ``self`` when one handler method calls another
    # (notably _maybe_adapt_speed), producing misleading TypeErrors.
    controller = object.__new__(handler)
    controller.service = service
    controller.client = client
    controller.loop = loop
    controller.status = status
    controller.announced_destinations = set()
    controller.airtime_budget = airtime_budget
    controller.message_budgets = {}
    controller.tx_lock = asyncio.Lock()
    controller.last_tx_at_ms = None
    controller.next_tx_not_before_ms = None
    controller.active_transaction_id = None
    controller.auto_speed = args.auto_speed
    server = ThreadingHTTPServer((args.ui_host, args.ui_port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"JS8Mail UI: http://{args.ui_host}:{args.ui_port}", flush=True)
    delay = 1.0
    query_scheduler = QueryScheduler()
    inbox_scheduler = QueryScheduler(base_delay_ms=1_800_000, max_delay_ms=21_600_000)
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
    # MID is only locally unique; the sender is part of the reassembly key.
    reassembly: dict[tuple[str, str], MultipartAccumulator] = {}
    pending_call_queries: list[PendingCallQuery] = []
    # (custodian, JS8Call message id) -> (next retry time, retry count).
    pending_retrievals: dict[tuple[str, int], tuple[int, int]] = {}
    max_retrieval_retries = 3
    retrieval_retry_delay_ms = 45_000
    capability_last_sent: dict[str, int] = {}
    retrieval_capability_last_sent: dict[str, int] = {}
    recent_query_answers: dict[str, int] = {}
    route_evidence_settle_until_ms: dict[str, int] = {}
    route_evidence_settle_logged: set[str] = set()
    # A targeted QUERY CALL normally receives an answer within one or two
    # JS8Call cycles. Keep enough context for that response without blocking
    # discovery for several minutes; the fallback defer below is never shorter
    # than this window.
    query_context_window_ms = 90_000

    async def return_capability_for_collected_message(
        original_sender: str,
        immediate_source: str,
        message_id: str,
        incoming_path: tuple[str, ...],
    ) -> None:
        """Return a CAP toward the original sender after custodian retrieval."""
        local_call = str(status.get("callsign", "")).strip().upper()
        original = original_sender.strip().upper()
        source = immediate_source.strip().upper()
        if not local_call or not original or original == local_call:
            return
        now = utc_now_ms()
        if now - retrieval_capability_last_sent.get(original, 0) < 60 * 60 * 1000:
            database.audit(
                "delivery.control",
                {
                    "label": "JS8Mail discovery already reported",
                    "target": original,
                    "status": "suppressed",
                    "detail": "Capability return is rate-limited to once per hour.",
                    "message_id": message_id,
                },
            )
            return
        reverse_path = reverse_custody_path(local_call, original, incoming_path)
        selected_path = reverse_path
        if not selected_path:
            plan = service.plan_route(local_call, original, band=str(status.get("band", "")))
            if len(plan.path) >= 3:
                selected_path = tuple(plan.path)
        if len(selected_path) >= 3:
            text = format_relay_text(selected_path, format_capability())
        else:
            selected_path = (local_call, original)
            text = f"{original} {format_capability()}"
        path_text = "→".join(selected_path)
        detail = (
            f"Message collected from {source}; returning JS8Mail capability toward "
            f"the original sender."
        )
        if not reverse_path:
            detail += " No recorded reverse path was available; this is a direct reachability attempt."
        database.audit(
            "delivery.control",
            {
                "label": "JS8Mail discovery · delivery confirmation",
                "target": original,
                "status": "started",
                "detail": detail,
                "path": path_text,
                "message_id": message_id,
            },
        )
        try:
            await controller.send_rf(text)
        except (ConnectionError, OSError, RuntimeError, AirtimeBudgetExceeded) as exc:
            database.audit(
                "delivery.control",
                {
                    "label": "JS8Mail discovery · delivery confirmation",
                    "target": original,
                    "status": "deferred",
                    "detail": f"Waiting to return capability: {type(exc).__name__}.",
                    "path": path_text,
                    "message_id": message_id,
                },
            )
            return
        retrieval_capability_last_sent[original] = now
        database.audit(
            "delivery.control",
            {
                "label": "JS8Mail discovery · delivery confirmation",
                "target": original,
                "status": "submitted",
                "detail": "Message was collected from a custodian; CAP queued for the original sender.",
                "path": path_text,
                "message_id": message_id,
            },
        )

    # A compact QUERY CALL response does not repeat the queried callsign.
    # Restore very recent contexts so a daemon restart between query and
    # response does not discard an otherwise useful positive answer.
    query_context_now = utc_now_ms()
    for audit in database.recent_audit_events(
        "discovery.query_submitted", query_context_now - QUERY_RESPONSE_MAX_MS
    ):
        payload = audit["payload"]
        action = str(payload.get("action", ""))
        if action not in {"candidate_query_call", "allcall_query_call"}:
            continue
        text_fields = str(payload.get("text", "")).strip().upper().split()
        if len(text_fields) < 4 or text_fields[-2:] == ["QUERY", "CALL"]:
            continue
        destination = text_fields[-1].rstrip("?")
        responder = str(payload.get("target", "")).strip().upper()
        if not destination or not responder:
            continue
        key = (
            f"call-query:{destination}"
            if responder == "@ALLCALL"
            else f"candidate-query:{responder}:{destination}"
        )
        pending_call_queries.append(
            PendingCallQuery(
                int(audit["created_at_ms"]),
                destination,
                responder,
                key,
                str(payload.get("band", "")),
                QUERY_RESPONSE_MAX_MS,
            )
        )

    def apply_radio_context(params: dict[str, Any]) -> None:
        band, dial_frequency = context_from_params(params)
        if band:
            status["band"] = band
        if dial_frequency is not None:
            status["dial_frequency"] = dial_frequency

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
        now_wall = utc_now_ms()
        pending_call_queries[:] = [
            query
            for query in pending_call_queries
            if now_wall - query.submitted_at_ms <= LATE_QUERY_CONTEXT_MS
        ]
        if route_destination is not None:
            responder = target.strip().upper()
            destination = route_destination.strip().upper()
            if responder == "@ALLCALL" and any(
                query.responder == "@ALLCALL"
                and query.destination != destination
                and now_wall - query.submitted_at_ms <= query.response_window_ms
                for query in pending_call_queries
            ):
                # A compact ALLCALL YES cannot identify which queried
                # destination it answers. Keep one outstanding ALLCALL
                # destination so a valid answer is never misrouted.
                return False
            if any(
                query.responder == responder and query.destination != destination
                for query in pending_call_queries
            ):
                # CALL YES does not repeat the destination. Keep at most one
                # outstanding destination per directed station/@ALLCALL.
                return False
        try:
            await controller.send_rf(text)
            database.audit(
                "discovery.query_submitted",
                {
                    "action": action,
                    "target": target,
                    "text": text,
                    "band": str(status.get("band", "")),
                },
            )
            scheduler.record(key, now)
            if route_destination is not None:
                response_window_ms = query_response_window_ms(
                    action, status.get("speed", 0)
                )
                pending_call_queries.append(
                    PendingCallQuery(
                        now_wall,
                        route_destination.strip().upper(),
                        target.strip().upper(),
                        key,
                        str(status.get("band", "")),
                        response_window_ms,
                    )
                )
                del pending_call_queries[:-16]
            return True
        except (ConnectionError, RuntimeError):
            scheduler.record(key, now)
            return False

    async def discovery_loop() -> None:
        inbox_key = "inbox:broadcast"
        last_prune_at_ms = 0
        last_context_refresh_at_ms = 0
        while True:
            await asyncio.sleep(5)
            now_wall_ms = utc_now_ms()
            if now_wall_ms - last_prune_at_ms >= 60 * 60 * 1000:
                database.prune_observations(now_ms=now_wall_ms)
                database.prune_groups(now_ms=now_wall_ms)
                last_prune_at_ms = now_wall_ms
            if not client.connected or args.tx_mode != "automatic" or status.get("paused"):
                continue
            if now_wall_ms - last_context_refresh_at_ms >= 15_000:
                try:
                    frequency = await client.request_read_only("RIG.GET_FREQ")
                    apply_radio_context(dict(frequency.params))
                    if not status.get("dial_frequency") and frequency.value.strip().isdigit():
                        status["dial_frequency"] = int(frequency.value.strip())
                        status["band"] = band_from_frequency_hz(int(frequency.value.strip()))
                except (ConnectionError, OSError, RuntimeError):
                    pass
                last_context_refresh_at_ms = now_wall_ms
            now = int(asyncio.get_running_loop().time() * 1000)
            if inbox_scheduler.due(inbox_key, now):
                await submit_query(
                    inbox_key,
                    messages_query(),
                    "messages_query",
                    "@ALLCALL",
                    scheduler=inbox_scheduler,
                )
            # Resolve one durable RF transaction at a time.  The parent
            # message is deliberately not used as the ACK correlation key:
            # it may have been moved back to route discovery after a timeout,
            # while the radio can still deliver a late ACK for the exact
            # transmission.
            for transaction in database.expire_transmission_transactions(now_wall_ms):
                message = database.get_message(str(transaction["message_id"]))
                if message is None or message["state"] in {
                    MessageState.STORED,
                    MessageState.DELIVERED,
                    MessageState.FAILED,
                    MessageState.EXPIRED,
                    MessageState.CANCELLED,
                }:
                    continue
                message_id = str(message["id"])
                operation = str(transaction["operation"])
                if operation == "store":
                    custodian = str(transaction["expected_responder"])
                    database.record_attempt(
                        message_id,
                        "store_timeout",
                        custodian,
                        "uncertain",
                        "no custodian ACK before response deadline; storage is unconfirmed",
                    )
                    database.upsert_custody(
                        message_id,
                        custodian,
                        "failed",
                        "no legacy JS8Call store ACK; message may still be stored",
                    )
                    if message["state"] == MessageState.IN_PROGRESS:
                        database.transition_message(message_id, MessageState.WAITING_ROUTE)
                    database.defer_message(
                        message_id,
                        15 * 60 * 1000,
                        "custodian ACK absent; waiting before another store offer",
                        increment_retry=False,
                    )
                else:
                    database.record_attempt(
                        message_id,
                        "delivery_timeout",
                        str(transaction["target"]),
                        "uncertain",
                        "no ACK before the operation response deadline",
                    )
                    if message["state"] == MessageState.IN_PROGRESS:
                        database.transition_message(message_id, MessageState.WAITING_ROUTE)
                    database.defer_message(
                        message_id,
                        2 * 60 * 1000,
                        "delivery ACK absent; route discovery will try another opportunity",
                    )
            for message in database.list_messages():
                if message["state"] not in {
                    MessageState.QUEUED,
                    MessageState.IN_PROGRESS,
                    MessageState.WAITING_ROUTE,
                }:
                    continue
                destination = str(message["destination"])
                expires_at_ms = message.get("expires_at_ms")
                if isinstance(expires_at_ms, int) and expires_at_ms <= utc_now_ms():
                    database.transition_message(str(message["id"]), MessageState.EXPIRED)
                    database.record_attempt(
                        str(message["id"]), "expiry", destination, "expired", "retry window elapsed"
                    )
                    continue
                if message["state"] == MessageState.QUEUED:
                    # A queued message may be restored after a daemon restart
                    # or an operator retry without passing through the HTTP
                    # request that normally starts preparation.
                    try:
                        await controller.prepare(str(message["id"]))
                    except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                        database.record_attempt(
                            str(message["id"]), "prepare", destination, "deferred", type(exc).__name__
                        )
                    continue
                if message["state"] == MessageState.IN_PROGRESS:
                    transactions = database.list_transmission_transactions(str(message["id"]))
                    active_transactions = [
                        item for item in transactions
                        if item["status"] in {"queued", "tx_active", "awaiting_ack"}
                    ]
                    if active_transactions:
                        # A new message or a discovery tick must never fill a
                        # receive window belonging to this transaction.
                        continue
                    if not any(item["status"] == "acknowledged" for item in transactions):
                        database.record_attempt(
                            str(message["id"]),
                            "reconcile",
                            str(message["destination"]),
                            "deferred",
                            "previous RF transaction ended without a durable ACK; returning to discovery",
                        )
                        database.transition_message(str(message["id"]), MessageState.WAITING_ROUTE)
                        database.defer_message(
                            str(message["id"]),
                            2 * 60 * 1000,
                            "previous transaction ended; retrying after a receive window",
                        )
                        continue
                direct_expired = False
                if message["state"] == MessageState.IN_PROGRESS:
                    attempts = database.list_attempts(str(message["id"]))
                    direct_submissions = [
                        attempt for attempt in attempts
                        if attempt["action"] in {"direct", "multipart"}
                        and attempt["status"] == "submitted"
                    ]
                    enhanced_message = bool(
                        database.list_message_parts(
                            str(message["id"]),
                            direction="outgoing",
                            peer=destination,
                        )
                    )
                    has_followup = any(
                        (
                            attempt["action"] in {"hop_ack", "delivery_ack"}
                            or (
                                attempt["action"] == "standard_ack"
                                and not enhanced_message
                            )
                        )
                        and attempt["status"] in {"received", "confirmed"}
                        for attempt in attempts
                    )
                    if direct_submissions and not has_followup:
                        last_direct = int(direct_submissions[-1]["created_at_ms"])
                        direct_expired = now_wall_ms - last_direct >= DIRECT_RESPONSE_DEADLINE_MS
                        if direct_expired:
                            database.record_attempt(
                                str(message["id"]), "direct_timeout", destination, "failed",
                                "no ACK or JS8Mail receipt within 2-minute direct deadline",
                            )
                            database.transition_message(str(message["id"]), MessageState.WAITING_ROUTE)
                            origin = str(status.get("callsign", "")).upper()
                            if origin:
                                try:
                                    speed = int(status.get("speed", 1))
                                except (TypeError, ValueError):
                                    speed = 0
                                database.record_link_outcome(
                                    origin,
                                    destination,
                                    speed if speed in SPEED_AIRTIME_MS else 0,
                                    None,
                                    False,
                                    str(status.get("band", "")),
                                )
                    # A relay-hop ACK proves custody of that hop, not final
                    # delivery. Do not retry during the short forwarding
                    # deadline, but do not leave the message permanently
                    # stuck in IN_PROGRESS if the relay never produces a
                    # final ACK/receipt either.
                    relay_hop_acks = [
                        attempt for attempt in attempts
                        if attempt["action"] == "standard_ack"
                        and attempt["status"] == "received"
                        and str(attempt["target"]).upper() != destination.upper()
                    ]
                    if relay_hop_acks and not any(
                        attempt["action"] == "delivery_ack"
                        and attempt["status"] in {"received", "confirmed"}
                        for attempt in attempts
                    ):
                        last_hop_ack = int(relay_hop_acks[-1]["created_at_ms"])
                        if now_wall_ms - last_hop_ack >= DIRECT_RESPONSE_DEADLINE_MS:
                            database.record_attempt(
                                str(message["id"]),
                                "relay_forward_timeout",
                                destination,
                                "failed",
                                "hop acknowledged but no final delivery evidence arrived",
                            )
                            database.transition_message(
                                str(message["id"]), MessageState.WAITING_ROUTE
                            )
                            database.wake_message_for_route(str(message["id"]))
                            continue
                direct_age_ms = service.recent_answered_age_ms(
                    destination,
                    str(status.get("callsign", "")),
                    band=str(status.get("band", "")),
                )
                heard_age_ms = service.recent_heard_age_ms(
                    destination,
                    band=str(status.get("band", "")),
                )
                message_id = str(message["id"])
                settle_until = route_evidence_settle_until_ms.get(message_id)
                if settle_until is not None and direct_age_ms is None:
                    if now_wall_ms < settle_until:
                        if message_id not in route_evidence_settle_logged:
                            database.record_attempt(
                                message_id,
                                "route_evidence_settling",
                                destination,
                                "waiting",
                                f"waiting {max(1, (settle_until - now_wall_ms) // 1000)}s for competing replies",
                            )
                            route_evidence_settle_logged.add(message_id)
                        continue
                    route_evidence_settle_until_ms.pop(message_id, None)
                    route_evidence_settle_logged.discard(message_id)
                    database.record_attempt(
                        message_id,
                        "route_evidence_settling",
                        destination,
                        "complete",
                        "reply collection window ended; selecting the best available path",
                    )
                if (
                    message["state"] == MessageState.WAITING_ROUTE
                    and (direct_age_ms is not None or heard_age_ms is not None)
                    and not direct_expired
                    and database.due_for_retry(str(message["id"]))
                ):
                    if message["state"] == MessageState.WAITING_ROUTE:
                        if direct_age_ms is not None:
                            route_detail = f"recent direct response, {direct_age_ms // 1000}s ago"
                        else:
                            assert heard_age_ms is not None
                            route_detail = (
                                f"recently heard, {heard_age_ms // 1000}s ago; "
                                "likely reachable, not confirmed"
                            )
                        database.record_attempt(
                            str(message["id"]), "route", destination, "available", route_detail
                        )
                        try:
                            future = asyncio.create_task(
                                controller.transmit(str(message["id"]))
                            )
                            await future
                        except (ConnectionError, OSError, RuntimeError, TypeError, ValueError) as exc:
                            if isinstance(exc, AirtimeBudgetExceeded) and exc.scope == "per-message-total":
                                database.record_attempt(
                                    str(message["id"]), "direct", destination, "failed",
                                    "one-hour per-message airtime ceiling reached",
                                )
                                database.transition_message(str(message["id"]), MessageState.FAILED)
                                continue
                            database.record_attempt(
                                str(message["id"]),
                                "direct",
                                destination,
                                "deferred",
                                f"local/API handoff deferred: {type(exc).__name__}",
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
                    plan = service.plan_route(
                        str(status.get("callsign", "")),
                        destination,
                        attempted_paths=database.attempted_message_paths(str(message["id"])),
                        band=str(status.get("band", "")),
                    )
                    if len(plan.path) >= 3:
                        database.record_attempt(
                            str(message["id"]),
                            "route",
                            destination,
                            "selected",
                            plan.explanation,
                        )
                        try:
                            await controller.transmit(str(message["id"]), plan)
                        except (ConnectionError, OSError, RuntimeError, TypeError, ValueError) as exc:
                            if isinstance(exc, AirtimeBudgetExceeded) and exc.scope == "per-message-total":
                                database.record_attempt(
                                    str(message["id"]), "relay", plan.path[1], "failed",
                                    "one-hour per-message airtime ceiling reached",
                                )
                                database.transition_message(str(message["id"]), MessageState.FAILED)
                                continue
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
                promising = service.promising_stations(
                    destination, band=str(status.get("band", ""))
                )[:3]
                if message.get("retry_count", 0) >= 3:
                    candidate_custodian = next((candidate for candidate in promising if candidate != destination), None)
                    active_custody = {
                        str(item["custodian"]).upper()
                        for item in database.list_custody(str(message["id"]))
                        if item["status"] in {"offered", "accepted", "retrieval_pending", "forwarded"}
                    }
                    custody_history = {
                        str(item["custodian"]).upper()
                        for item in database.list_custody(str(message["id"]))
                    }
                    # Legacy custodians do not provide a portable end-to-end
                    # receipt. Limit the number of distinct offers and never
                    # offer a second custodian while an earlier one is still
                    # pending or accepted.
                    if (
                        candidate_custodian is not None
                        and candidate_custodian.upper() not in active_custody
                        and len(custody_history) < 3
                    ):
                        try:
                            await controller.transmit_store(str(message["id"]), candidate_custodian)
                        except (ConnectionError, OSError, RuntimeError, TypeError, ValueError) as exc:
                            if isinstance(exc, AirtimeBudgetExceeded) and exc.scope == "per-message-total":
                                database.record_attempt(
                                    str(message["id"]), "store", candidate_custodian, "failed",
                                    "one-hour per-message airtime ceiling reached",
                                )
                                database.transition_message(str(message["id"]), MessageState.FAILED)
                                continue
                            database.record_attempt(
                                str(message["id"]), "store", candidate_custodian, "deferred", type(exc).__name__
                            )
                            database.defer_message(str(message["id"]), 60_000, "custodian offer unavailable")
                        continue
                if message["state"] == MessageState.WAITING_ROUTE and not database.due_for_retry(
                    str(message["id"])
                ):
                    # A defer deadline is authoritative even when fresh
                    # indirect evidence exists. QueryScheduler controls when
                    # the next targeted query is allowed; do not append a new
                    # message defer every five-second loop iteration.
                    continue
                call_key = f"call-query:{destination}"
                query_submitted = False
                query_wait_ms = query_context_window_ms
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
                                query_submitted = query_submitted or candidate_submitted
                                if candidate_submitted:
                                    query_wait_ms = max(
                                        query_wait_ms,
                                        query_response_window_ms(
                                            "candidate_query_call", status.get("speed", 0)
                                        ),
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
                        query_submitted = allcall_submitted
                        if allcall_submitted:
                            query_wait_ms = query_response_window_ms(
                                "allcall_query_call", status.get("speed", 0)
                            )
                delay_ms = min(
                    60_000 * (2 ** min(int(message.get("retry_count", 0)), 8)),
                    21_600_000,
                )
                defer_detail = (
                    f"query submitted; awaiting response for up to "
                    f"{query_wait_ms // 1000} seconds; "
                    f"fallback discovery in "
                    f"{max(delay_ms, query_wait_ms) // 1000} seconds"
                    if query_submitted
                    else f"no current route; discovery will retry in {delay_ms // 60000} minute(s)"
                )
                database.defer_message(
                    str(message["id"]),
                    max(delay_ms, query_wait_ms) if query_submitted else delay_ms,
                    defer_detail,
                )

    async def discovery_supervisor() -> None:
        """Keep discovery alive and make unexpected failures auditable."""
        while True:
            try:
                await discovery_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - scheduler must survive unexpected adapter/data errors
                database.audit(
                    "discovery.loop_error",
                    {"error": type(exc).__name__, "detail": str(exc)[:160]},
                )
                await asyncio.sleep(1)

    discovery_task = asyncio.create_task(discovery_supervisor())
    try:
        while True:
            try:
                await client.connect()
                status["connected"] = True
                database.audit("js8call.connected", {"host": args.host, "port": args.port})
                delay = 1.0

                async def handle(event: NormalizedEvent) -> None:
                    apply_radio_context(dict(event.params))
                    # RIG.PTT is the authoritative live TX/RX transition. A
                    # TX.FRAME event proves a frame was produced, but may be
                    # followed by a delayed or missing UI refresh; using it
                    # alone leaves the TX LED stuck on.
                    if event.event_type == "RIG.PTT":
                        ptt = event.params.get("PTT")
                        previous_activity = status.get("radio_activity")
                        status["radio_activity"] = "TX" if ptt is True or str(event.value).lower() == "on" else "RX"
                        active_transaction_id = controller.active_transaction_id
                        if status["radio_activity"] == "TX" and active_transaction_id is not None:
                            database.mark_transmission_active_by_id(active_transaction_id)
                        if status["radio_activity"] == "RX":
                            if active_transaction_id is not None and previous_activity == "TX":
                                database.finish_transmission(active_transaction_id)
                                controller.active_transaction_id = None
                            status["tx_message_id"] = None
                            if previous_activity == "TX":
                                # The real TX end is stronger evidence than
                                # the API submission estimate. Keep a full
                                # response window after it before another
                                # automated request is admitted.
                                controller.next_tx_not_before_ms = utc_now_ms() + AUTOMATED_RX_WINDOW_MS
                    elif event.event_type.startswith("TX"):
                        status["radio_activity"] = "TX"
                    # The documented TCP API does not currently expose a
                    # generic "decode cycle finished" event. RX result events
                    # are therefore the strongest portable DCD evidence. The
                    # aliases below also support builds which forward the
                    # internal decode-complete notification.
                    decode_events = {
                        "RX.ACTIVITY", "RX.DIRECTED", "RX.SPOT", "RX.DECODE",
                        "RX.DECODE_FINISHED", "RX.DCD", "DECODE.FINISHED",
                    }
                    if event.event_type in decode_events:
                        status["dcd_until_ms"] = utc_now_ms() + 1_000
                    if "J8M" in event.value.upper() or "JS8MAIL" in event.value.upper():
                        status["js8_activity_until_ms"] = utc_now_ms() + 1_000
                    database.record_observation(
                        event,
                        band=str(status.get("band", "")),
                        dial_frequency=status.get("dial_frequency"),
                    )
                    database.record_link_projection(
                        event,
                        band=str(status.get("band", "")),
                        dial_frequency=status.get("dial_frequency"),
                    )
                    for group in extract_groups(event.value, *[str(value) for value in event.params.values()]):
                        database.observe_group(group, default_group_description(group))
                    frame = normalize_directed_event(event) if event.event_type.startswith("RX.DIRECTED") else None
                    ack = parse_ack(frame.payload) if frame is not None else None
                    source = frame.source if frame is not None else event.params.get("FROM")
                    command = frame.command if frame is not None else ""
                    message_text = frame.payload if frame is not None else ""
                    resend = parse_resend_request(frame.payload) if frame is not None else None
                    if resend is not None and isinstance(source, str):
                        request_id, total, missing = resend
                        requested_message = database.get_message(request_id)
                        authorized = False
                        if requested_message is not None:
                            authorized = (
                                str(requested_message["destination"]).upper() == source.upper()
                                or any(
                                    item["custodian"].upper() == source.upper()
                                    and item["status"] in {"accepted", "retrieval_pending", "forwarded"}
                                    for item in database.list_custody(request_id)
                                )
                                or any(source.upper() in {call.upper() for call in path} for path in database.message_paths(request_id))
                            )
                        if authorized:
                            try:
                                if requested_message is None:
                                    raise ValueError("unknown multipart message")
                                parts = split_human_message(request_id, str(requested_message["body"]))
                                if total != len(parts):
                                    raise ValueError("multipart request total does not match stored message")
                                raw_path = str(event.params.get("PATH", ""))
                                request_path = tuple(item.upper() for item in raw_path.split(">") if item)
                                for number in missing:
                                    if number <= len(parts):
                                        payload = format_human_data_part(
                                            parts[number - 1],
                                            str(status.get("callsign", "")).upper(),
                                            str(requested_message["destination"]).upper(),
                                        )
                                        text = (
                                            format_relay_message(request_path, payload)
                                            if len(request_path) >= 3
                                            else f"{source} {payload}"
                                        )
                                        await controller.send_rf(text, request_id)
                                database.record_attempt(
                                    request_id, "part_resend", source, "submitted",
                                    f"served {len(missing)} requested part(s) through custody path",
                                )
                            except (ValueError, RuntimeError, ConnectionError):
                                database.record_attempt(request_id, "part_resend", source, "failed", "unable to serve request")
                    available_id = parse_messages_available(frame.wire_text if frame is not None else "")
                    if available_id is not None and isinstance(source, str):
                        retrieval_key = (source.upper(), available_id)
                        if retrieval_key not in pending_retrievals:
                            try:
                                await controller.send_rf(retrieve_message_query(source, available_id))
                                pending_retrievals[retrieval_key] = (utc_now_ms(), 1)
                                database.audit(
                                    "inbox.retrieval_submitted",
                                    {"custodian": source.upper(), "js8call_message_id": available_id},
                                )
                            except (ConnectionError, RuntimeError):
                                database.audit(
                                    "inbox.retrieval_failed",
                                    {"custodian": source.upper(), "js8call_message_id": available_id},
                                )
                    capability = parse_capability(frame.payload if frame is not None else "")
                    if capability is not None and isinstance(source, str):
                        version, features = capability
                        capability_now = utc_now_ms()
                        database.upsert_peer_capabilities(
                            source, version, features, capability_now + CAPABILITY_TTL_MS
                        )
                        for pending_message in database.list_messages(MessageState.WAITING_ROUTE):
                            if str(pending_message["destination"]).upper() == source.upper():
                                database.wake_message_for_route(str(pending_message["id"]))
                        # CAP is a request/response hint, not an endlessly
                        # echoed heartbeat. One reply per peer per hour is
                        # enough to establish capability and prevents loops.
                        last_capability = capability_last_sent.get(source.upper(), 0)
                        if capability_now - last_capability < 60 * 60 * 1000:
                            capability = None
                        else:
                            capability_last_sent[source.upper()] = capability_now
                    if capability is not None and isinstance(source, str):
                        try:
                            await controller.send_rf(f"{source} {format_capability(features)}")
                            database.audit(
                                "peer.capability_ack_submitted",
                                {"peer": source.upper(), "version": version},
                            )
                        except (ConnectionError, RuntimeError):
                            database.audit("peer.capability_ack_failed", {"peer": source.upper()})
                    # A group-directed MSG is useful alert traffic even when
                    # no JS8Mail peer is present. Preserve it in the separate
                    # group-alert inbox; @ALLCALL is deliberately excluded
                    # because ordinary CQ/query traffic is not mail.
                    if (
                        frame is not None
                        and isinstance(source, str)
                        and command == "MSG"
                        and frame.destination.startswith("@")
                        and frame.destination != "@ALLCALL"
                        and message_text.strip()
                    ):
                        group_id = "group-" + hashlib.sha256(
                            f"{frame.destination}\n{source.upper()}\n{message_text}".encode()
                        ).hexdigest()[:16]
                        database.upsert_inbox_message(
                            source,
                            group_id,
                            message_text.strip(),
                            1,
                            (1,),
                            True,
                            tuple(str(event.params.get("PATH", source)).split(">")),
                            frame.destination,
                            delivery="group_broadcast",
                        )
                    # Legacy JS8Call messages arrive without a JS8Mail ID.
                    # Store them too, using a deterministic local fingerprint
                    # so repeated custodian retrieval does not create copies.
                    local_call = str(status.get("callsign", "")).upper()
                    if (
                        isinstance(source, str)
                        and command in {"MSG", "MSG TO:"}
                        and message_text.strip()
                        and not message_text.startswith("J8M1 ")
                        and frame is not None
                        and frame.destination == local_call
                    ):
                        if command == "MSG TO:" and frame.stored_recipient.upper() not in {
                            local_call,
                            "",
                        } and not frame.stored_recipient.startswith("@"):
                            database.audit(
                                "custody.inbound_accepted",
                                {
                                    "custodian": local_call,
                                    "recipient": frame.stored_recipient,
                                    "sender": source.upper(),
                                },
                            )
                            # JS8Call also persists this in its own store. It
                            # is not an operator inbox message for us.
                            message_text = ""
                        if not message_text:
                            return
                        else:
                            collected = any(key[0] == source.upper() for key in pending_retrievals)
                            original_sender = source
                            if collected:
                                # JS8Call's stored-message response normally
                                # preserves the origin in its structured
                                # fields or as an origin-prefixed MSG line.
                                # Prefer that over the immediate custodian.
                                for field in (
                                    "ORIGINAL_SENDER",
                                    "ORIGINAL_FROM",
                                    "ORIGIN",
                                ):
                                    candidate = str(event.params.get(field, "")).strip().upper()
                                    if re.fullmatch(r"[A-Z0-9/]{1,16}", candidate or ""):
                                        original_sender = candidate
                                        break
                                leading_origin = re.match(
                                    r"^([A-Z0-9/]{1,16})\s+MSG(?:\s+TO:\s*[^\s]+)?\s+(.*)$",
                                    message_text,
                                    re.IGNORECASE,
                                )
                                if leading_origin is not None:
                                    original_sender = leading_origin.group(1).upper()
                                    message_text = leading_origin.group(2).strip()
                            retrieved = re.search(
                                r"\s+FROM\s+([A-Z0-9/]{1,16})\s*$",
                                message_text,
                                re.IGNORECASE,
                            )
                            if retrieved is not None:
                                original_sender = retrieved.group(1).upper()
                                message_text = message_text[: retrieved.start()].rstrip()
                        partial = (
                            not frame.final
                            or bool(re.search(r"(?:…|\.{3,})\s*$", message_text))
                        )
                        partial_id = database.find_partial_inbox(source, message_text)
                        legacy_id = partial_id or (
                            "legacy-partial-" + hashlib.sha256(
                                f"{original_sender.upper()}\n{message_text.rstrip('… .')}".encode()
                            ).hexdigest()[:16]
                            if partial
                            else "legacy-" + hashlib.sha256(
                                f"{original_sender.upper()}\n{message_text}".encode()
                            ).hexdigest()[:16]
                        )
                        database.upsert_inbox_message(
                            original_sender,
                            legacy_id,
                            message_text.strip(),
                            1,
                            () if partial else (1,),
                            not partial,
                            tuple(str(event.params.get("PATH", source)).split(">")),
                            frame.stored_recipient if command == "MSG TO:" else "",
                            delivery="stored_collected" if collected else "direct",
                        )
                        if collected and original_sender.upper() != local_call:
                            incoming_path = tuple(
                                item.upper()
                                for item in str(event.params.get("PATH", source)).split(">")
                                if item.strip()
                            )
                            await return_capability_for_collected_message(
                                original_sender,
                                source,
                                legacy_id,
                                incoming_path,
                            )
                        # A JS8Mail receiver can passively reveal its
                        # capability after receiving ordinary directed mail.
                        # This gives an Opportunistic sender a safe clue for
                        # the next message without placing CAP before the
                        # current message or competing with its ACK. Keep it
                        # strictly rate-limited.
                        capability_now = utc_now_ms()
                        capability_peer = source.upper()
                        if collected:
                            # The collected-message branch above returns the
                            # capability toward the original sender. Do not
                            # also advertise only to the custodian.
                            capability_peer = local_call
                        if (
                            capability_peer != local_call
                            and capability_now - capability_last_sent.get(capability_peer, 0)
                            >= 60 * 60 * 1000
                        ):
                            try:
                                await controller.send_rf(f"{capability_peer} {format_capability()}")
                                capability_last_sent[capability_peer] = capability_now
                                database.audit(
                                    "peer.capability_advertisement_submitted",
                                    {"peer": capability_peer, "reason": "ordinary_message_received"},
                                )
                            except (ConnectionError, RuntimeError):
                                database.audit(
                                    "peer.capability_advertisement_deferred",
                                    {"peer": capability_peer, "reason": "ordinary_message_received"},
                                )
                        matching_retrievals = [
                            (key, state) for key, state in pending_retrievals.items()
                            if key[0] == source.upper()
                        ]
                        if partial and matching_retrievals:
                            key, (next_retry_at, retry_count) = matching_retrievals[0]
                            now = utc_now_ms()
                            if retry_count < max_retrieval_retries and now >= next_retry_at:
                                retry_at = now + retrieval_retry_delay_ms
                                pending_retrievals[key] = (retry_at, retry_count + 1)

                                async def retry_partial_retrieval(
                                    custodian: str = source.upper(),
                                    stored_id: int = key[1],
                                    message_id: str = legacy_id,
                                    attempt: int = retry_count + 1,
                                ) -> None:
                                    await asyncio.sleep(retrieval_retry_delay_ms / 1000)
                                    try:
                                        await controller.send_rf(retrieve_message_query(custodian, stored_id))
                                        database.record_attempt(
                                            message_id, "inbox_retrieval", custodian, "submitted",
                                            f"re-requested JS8Call message {stored_id} after partial decode (attempt {attempt})",
                                        )
                                    except (ConnectionError, RuntimeError):
                                        database.audit(
                                            "inbox.retrieval_retry_failed",
                                            {"custodian": custodian, "js8call_message_id": stored_id},
                                        )

                                asyncio.create_task(retry_partial_retrieval())
                        elif not partial:
                            for key in tuple(pending_retrievals):
                                if key[0] == source.upper():
                                    pending_retrievals.pop(key, None)
                    query_response = parse_query_call_response(frame.wire_text if frame is not None else "")
                    if (
                        query_response is not None
                        and isinstance(source, str)
                        and local_call
                        # The structured TO field carries our callsign, while
                        # the compact YES reply normally omits it from TEXT.
                        # A parsed recipient is therefore optional here.
                        and query_response.recipient in {None, local_call}
                        and command == "YES"
                    ):
                        now = utc_now_ms()
                        pending_call_queries[:] = [
                            query
                            for query in pending_call_queries
                            if now - query.submitted_at_ms <= LATE_QUERY_CONTEXT_MS
                        ]
                        matched_query = correlate_query_call_response(
                            pending_call_queries,
                            source,
                            now_ms=now,
                            band=str(status.get("band", "")),
                            max_age_ms=QUERY_RESPONSE_MAX_MS,
                        )
                        late_response = False
                        if matched_query is None:
                            # A delayed compact YES is still useful when it
                            # can be mapped unambiguously to one recent query.
                            # Keep this bounded to avoid attributing stale
                            # AllCall traffic to a newer message.
                            matched_query = correlate_query_call_response(
                                pending_call_queries,
                                source,
                                now_ms=now,
                                band=str(status.get("band", "")),
                                max_age_ms=LATE_QUERY_CONTEXT_MS,
                                allow_late=True,
                            )
                            late_response = matched_query is not None
                        if matched_query is not None:
                            queried_destination = matched_query.destination
                            # RX.ACTIVITY and RX.DIRECTED may expose partial
                            # and completed forms of the same compact answer.
                            # Since YES does not echo the query destination,
                            # suppress another answer from this source briefly
                            # rather than risk assigning a duplicate to a
                            # different outstanding @ALLCALL query.
                            answer_key = f"{source.upper()}:{matched_query.destination if matched_query else ''}"
                            for answer_key_old, answered_at in list(recent_query_answers.items()):
                                if now - answered_at >= query_context_window_ms:
                                    recent_query_answers.pop(answer_key_old, None)
                            if now - recent_query_answers.get(answer_key, 0) < 30_000:
                                matched_query = None
                            else:
                                recent_query_answers[answer_key] = now
                        if matched_query is not None:
                            snr = query_response.snr
                            age_minutes = query_response.age_minutes
                            observed_at = int(now - ((age_minutes or 0) * 60_000))
                            remote_params: dict[str, Any] = {
                                "FROM": source.upper(),
                                "TO": queried_destination,
                                "EVIDENCE": "remote_query_call_yes",
                            }
                            if snr is not None:
                                remote_params["SNR"] = snr
                            if age_minutes is not None:
                                remote_params["AGE_MIN"] = age_minutes
                            remote_link = NormalizedEvent(
                                "QUERY.CALL.RESPONSE",
                                event.value,
                                remote_params,
                                observed_at,
                            )
                            database.record_observation(
                                remote_link,
                                band=str(status.get("band", "")),
                                dial_frequency=status.get("dial_frequency"),
                            )
                            database.record_link_projection(
                                remote_link,
                                band=str(status.get("band", "")),
                                dial_frequency=status.get("dial_frequency"),
                            )
                            if local_call and local_call != source.upper():
                                local_snr = event.params.get("SNR")
                                reachability_params: dict[str, Any] = {
                                    "FROM": local_call,
                                    "TO": source.upper(),
                                    "EVIDENCE": "query_answered",
                                }
                                if isinstance(local_snr, (int, float)):
                                    reachability_params["SNR"] = local_snr
                                reachability_link = NormalizedEvent(
                                    "QUERY.CALL.REACHABILITY",
                                    event.value,
                                    reachability_params,
                                    now,
                                )
                                database.record_observation(
                                    reachability_link,
                                    band=str(status.get("band", "")),
                                    dial_frequency=status.get("dial_frequency"),
                                )
                                database.record_link_projection(
                                    reachability_link,
                                    band=str(status.get("band", "")),
                                    dial_frequency=status.get("dial_frequency"),
                                )
                            query_scheduler.record(
                                matched_query.scheduler_key,
                                int(asyncio.get_running_loop().time() * 1000),
                                success=True,
                            )
                            if matched_query.responder != "@ALLCALL":
                                pending_call_queries.remove(matched_query)
                            for message in database.list_messages(MessageState.WAITING_ROUTE):
                                if str(message["destination"]).upper() == queried_destination.upper():
                                    message_id = str(message["id"])
                                    if message_id not in route_evidence_settle_until_ms:
                                        settle_ms = route_evidence_settling_window_ms(
                                            matched_query.response_window_ms,
                                            status.get("speed", 0),
                                        )
                                        route_evidence_settle_until_ms[message_id] = now + settle_ms
                                        database.record_attempt(
                                            message_id,
                                            "route_evidence_settling",
                                            queried_destination,
                                            "waiting",
                                            f"collecting competing query replies for up to {settle_ms // 1000}s",
                                        )
                                    evidence_detail = (
                                        f"confirmed reachability to {queried_destination}"
                                        if snr is None
                                        else f"heard {queried_destination} at {snr} dB"
                                    )
                                    if age_minutes is not None:
                                        evidence_detail += f", {age_minutes} minute(s) ago"
                                    if late_response:
                                        evidence_detail += "; delayed query response"
                                    database.record_attempt(
                                        message_id,
                                        "route_evidence",
                                        source.upper(),
                                        "received",
                                        evidence_detail,
                                    )
                                    database.wake_message_for_route(message_id)
                    # A direct SNR response is the answer to the inexpensive
                    # reachability probe. Do not wait for the full defer
                    # interval before using it, but still let the single RF
                    # arbiter decide when the next payload may go out.
                    if (
                        frame is not None
                        and frame.command in {"SNR", "YES"}
                        and source
                        and frame.destination == local_call
                    ):
                        for message in database.list_messages(MessageState.WAITING_ROUTE):
                            if str(message["destination"]).upper() == source.upper():
                                database.record_attempt(
                                    str(message["id"]),
                                    "route_evidence",
                                    source.upper(),
                                    "received",
                                    "direct reachability response",
                                )
                                database.wake_message_for_route(str(message["id"]))
                    legacy_ack = parse_legacy_ack(frame) if frame is not None else None
                    if isinstance(source, str) and legacy_ack is not None:
                        ack_responder, ack_path = legacy_ack
                        matched = _recent_outbound_transaction(database, ack_responder, utc_now_ms())
                        if matched is not None:
                            message, transaction = matched
                            message_id = str(message["id"])
                            database.acknowledge_transmission(int(transaction["id"]))
                            enhanced_message = bool(
                                database.list_message_parts(
                                    message_id,
                                    direction="outgoing",
                                    peer=str(message["destination"]),
                                )
                            )
                            operation = str(transaction["operation"])
                            if operation == "store":
                                database.upsert_custody(
                                    message_id, ack_responder, "accepted", "standard JS8Call store ACK"
                                )
                                database.record_attempt(
                                    message_id, "custody_ack", ack_responder, "received",
                                    "stored at custodian; recipient retrieval and delivery remain unproven",
                                )
                                if message["state"] not in {
                                    MessageState.STORED, MessageState.DELIVERED,
                                    MessageState.FAILED, MessageState.EXPIRED,
                                    MessageState.CANCELLED,
                                }:
                                    database.transition_message(message_id, MessageState.STORED)
                            else:
                                destination = str(message["destination"]).upper()
                                detail = (
                                    "standard JS8Call ACK; complete MSG accepted by destination inbox"
                                    if ack_responder == destination
                                    else "standard JS8Call final ACK returned through relay path"
                                )
                                if len(ack_path) > 1:
                                    detail += f" via {'→'.join(ack_path)}"
                                database.record_attempt(
                                    message_id, "standard_ack", ack_responder, "received", detail
                                )
                            if (
                                operation != "store"
                                and ack_responder == destination
                                and message["state"] not in {
                                    MessageState.STORED, MessageState.DELIVERED,
                                    MessageState.FAILED, MessageState.EXPIRED,
                                    MessageState.CANCELLED,
                                }
                                and not enhanced_message
                            ):
                                database.transition_message(message_id, MessageState.DELIVERED)
                                origin = str(status.get("callsign", "")).upper()
                                if origin:
                                    try:
                                        speed = int(event.params.get("SPEED", status.get("speed", 0)))
                                    except (TypeError, ValueError):
                                        speed = 0
                                    ack_snr = event.params.get("SNR")
                                    database.record_link_outcome(
                                        origin,
                                        ack_responder,
                                        speed if speed in SPEED_AIRTIME_MS else 0,
                                        float(ack_snr) if isinstance(ack_snr, (int, float)) else None,
                                        True,
                                        str(status.get("band", "")),
                                    )
                    if ack and isinstance(source, str):
                        kind, message_id, bitmap = ack
                        known_capabilities = database.peer_capabilities(source)
                        inferred_features = set(known_capabilities[1] if known_capabilities else ())
                        inferred_features.update(("E2E",) if kind == "delivered" else ("MP", "PA"))
                        database.upsert_peer_capabilities(
                            source,
                            known_capabilities[0] if known_capabilities else 1,
                            tuple(sorted(inferred_features)),
                            utc_now_ms() + CAPABILITY_TTL_MS,
                        )
                        receipt_message = database.get_message(message_id)
                        if receipt_message is not None:
                            if kind == "delivered":
                                metadata = parse_delivery_ack(frame.payload if frame is not None else "")
                                receipt_path = metadata[2] if metadata is not None else ()
                                destination_matches = source.upper() == str(receipt_message["destination"]).upper()
                                destination_name = str(receipt_message["destination"]).upper()
                                custody_rows = [
                                    item for item in database.list_custody(message_id)
                                    if item["status"] in {"accepted", "retrieval_pending", "forwarded"}
                                ]
                                receipt_nodes = {item.upper() for item in receipt_path}
                                forwarding_custodians = [
                                    item for item in custody_rows
                                    if item["custodian"].upper() in receipt_nodes
                                    and item["custodian"].upper() != destination_name
                                ]
                                # A final destination receipt may arrive from
                                # the destination itself, rather than from the
                                # custodian that forwarded it.  Correlate every
                                # proven custodian named in the receipt path.
                                forwarded_matches = bool(
                                    forwarding_custodians
                                    and destination_name in receipt_nodes
                                )
                                if destination_matches or forwarded_matches:
                                    detail = "end-to-end receipt"
                                    if metadata is not None:
                                        _, delivered_at_ms, path = metadata
                                        detail = f"delivered_at={delivered_at_ms}; path={'→'.join(path)}"
                                    database.record_attempt(message_id, "delivery_ack", source, "received", detail)
                                    if forwarded_matches:
                                        for custody_row in forwarding_custodians:
                                            custodian = str(custody_row["custodian"])
                                            database.upsert_custody(
                                                message_id, custodian, "forwarded",
                                                f"final receipt path includes {destination_name}",
                                            )
                                            database.record_attempt(
                                                message_id, "custodian_forwarded", custodian,
                                                "confirmed", detail,
                                            )
                                    if receipt_message["state"] not in {
                                        MessageState.STORED,
                                        MessageState.DELIVERED,
                                        MessageState.FAILED,
                                        MessageState.EXPIRED,
                                        MessageState.CANCELLED,
                                    }:
                                        database.transition_message(message_id, MessageState.DELIVERED)
                            else:
                                part_ack = parse_part_ack(frame.payload if frame is not None else "")
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
                                                resend_path = next(
                                                    (
                                                        route for route in database.message_paths(message_id)
                                                        if route[-1].upper() == source.upper()
                                                    ),
                                                    (),
                                                )
                                                resend_payload = format_human_data_part(
                                                    parts[number - 1],
                                                    str(status.get("callsign", "")).upper(),
                                                    str(receipt_message["destination"]).upper(),
                                                )
                                                resend_text = (
                                                    format_relay_message(resend_path, resend_payload)
                                                    if len(resend_path) >= 3
                                                    else f"{source} {resend_payload}"
                                                )
                                                await controller.send_rf(resend_text, message_id)
                                        database.record_attempt(
                                            message_id, "part_resend", source, "submitted", detail
                                        )
                                    except (ValueError, RuntimeError, ConnectionError):
                                        database.record_attempt(
                                            message_id, "part_resend", source, "failed", detail
                                        )
                                database.record_attempt(message_id, "hop_ack", source, "received", detail)
                    parsed_part = parse_human_data_part(frame.payload) if frame is not None else None
                    if parsed_part is not None and isinstance(source, str) and source.upper() != status["callsign"]:
                        part, envelope_origin, envelope_destination = parsed_part
                        # A valid JS8Mail data part is passive proof that this
                        # peer understands at least multipart framing. Do not
                        # infer E2E/PA from data alone; explicit CAP/receipts
                        # remain authoritative for those features.
                        known_capabilities = database.peer_capabilities(source)
                        inferred_features = set(known_capabilities[1] if known_capabilities else ())
                        inferred_features.add("MP")
                        database.upsert_peer_capabilities(
                            source,
                            known_capabilities[0] if known_capabilities else 1,
                            tuple(sorted(inferred_features)),
                            utc_now_ms() + CAPABILITY_TTL_MS,
                        )
                        if envelope_destination and envelope_destination.upper() != local_call:
                            # The surrounding JS8Call address and the
                            # explicit final destination disagree; do not
                            # turn a misaddressed frame into an inbox item.
                            parsed_part = None
                        else:
                            try:
                                logical_sender = (envelope_origin or source).upper()
                                reassembly_key = (logical_sender, part.message_id)
                                accumulator = reassembly.setdefault(
                                    reassembly_key, MultipartAccumulator(part.message_id, part.total)
                                )
                                if not accumulator.receipt().received:
                                    for stored_part in database.list_message_parts(
                                        part.message_id, direction="incoming", peer=logical_sender
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
                                    peer=logical_sender,
                                )
                                receipt = accumulator.receipt()
                                route = tuple(
                                    item.strip().upper()
                                    for item in str(event.params.get("PATH", "")).split(">")
                                    if item.strip()
                                )
                                local_call = str(status.get("callsign", "")).upper()
                                reverse_route = tuple(reversed(route))
                                if local_call not in reverse_route:
                                    reverse_route = ()

                                def reply_text(body: str) -> str:
                                    if len(reverse_route) >= 3 and reverse_route[0] == local_call:
                                        return format_relay_text(reverse_route, body)
                                    return f"{logical_sender} {body}"

                                database.upsert_inbox_message(
                                    logical_sender,
                                    part.message_id,
                                    accumulator.partial_preview(),
                                    part.total,
                                    receipt.received,
                                    receipt.complete,
                                    route or (source,),
                                    envelope_destination if envelope_destination and envelope_destination.startswith("@") else "",
                                    protocol="js8m",
                                    delivery="forwarded" if len(route) >= 3 else "direct",
                                )
                                if accumulator.should_ack(utc_now_ms()):
                                    await controller.send_rf(reply_text(format_part_ack(receipt)))
                                    database.audit("message.part_ack_submitted", {"message_id": part.message_id, "to": logical_sender})
                                if receipt.complete:
                                    await controller.send_rf(
                                        reply_text(
                                            format_delivery_ack(
                                                part.message_id,
                                                utc_now_ms(),
                                                route or (local_call, logical_sender),
                                            )
                                        )
                                    )
                                    database.audit("message.delivered_ack_submitted", {"message_id": part.message_id, "to": logical_sender})
                            except (ValueError, RuntimeError, ConnectionError):
                                database.audit("message.ack_failed", {"source": source})

                reader_task = asyncio.create_task(client.read_events(handle))
                try:
                    identity = await client.request_read_only("STATION.GET_CALLSIGN")
                    status["callsign"] = identity.value.strip().upper()
                    try:
                        frequency = await client.request_read_only("RIG.GET_FREQ")
                        apply_radio_context(dict(frequency.params))
                        if not status.get("dial_frequency"):
                            value = frequency.value.strip()
                            if value.isdigit():
                                status["dial_frequency"] = int(value)
                                status["band"] = band_from_frequency_hz(int(value))
                    except (ConnectionError, OSError, RuntimeError):
                        pass
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
    parser.add_argument(
        "--tx-mode",
        choices=("observe", "automatic"),
        default="automatic",
        help="RF handoff mode (default: automatic; use observe for receive-only)",
    )
    parser.add_argument(
        "--auto-speed",
        action="store_true",
        help="Allow the adapter to request evidence-backed JS8Call speed changes",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
