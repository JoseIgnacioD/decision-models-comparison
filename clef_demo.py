# ============================================================
#  Chat AI vs Decision AI  -  local version (runs on your computer)
#  Uses the Cloudflare REST API with your account ID and API token.
#  Optional: also compares Microsoft-Decision-1 on your own Azure deployment.
#  Needs only Python 3.8 or newer. No extra installs.
#
#  Run it:   python clef_demo.py
#  Then open http://localhost:8787 in your browser.
# ============================================================
import json, os, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- Your Cloudflare details --------------------------------------
# Best: set them as environment variables CLOUDFLARE_ACCOUNT_ID and
# CLOUDFLARE_API_TOKEN. Or write them between the quotes below.
# Never share this file once your token is written in it.
ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")

# ---- Optional: Microsoft-Decision-1 on Azure (Microsoft Foundry) ----
# Leave empty to show only the chat AI and Clef. Copy these values from
# your deployment's details page in the Foundry portal.
AZURE_ENDPOINT = os.environ.get("AZURE_ENDPOINT", "").rstrip("/")
AZURE_DEPLOYMENT_NAME = os.environ.get("AZURE_DEPLOYMENT_NAME", "")
AZURE_API_KEY = os.environ.get("AZURE_API_KEY", "")

# ---- Settings you can change --------------------------------------
PORT = 8787
CLEF_MODEL = "clef-flash"  # "clef-flash" (faster) or "clef" (bigger, more precise)
CHAT_MODELS = [            # tried in order until one works
    "@cf/meta/llama-3.1-8b-instruct",
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
]
# --------------------------------------------------------------------

TEAMS = {
    "it": "IT: computers, Wi-Fi, projectors and online tools",
    "kitchen": "Kitchen: food and the cafeteria",
    "nurse": "Nurse: health, injuries and feeling unwell",
    "office": "Office: dates, trips, documents and payments",
    "teacher": "Teacher: homework, grades and lessons",
}
MOODS = ["Calm", "A bit upset", "Very upset"]

CHAT_PROMPT = (
    "You are a helpful assistant at a school. Read the message from a student or parent and answer three questions: "
    "1) Is it urgent (does it need action today)? "
    "2) Who should handle it: IT, Kitchen, Nurse, Office or Teacher? "
    "3) How upset is the person: calm, a bit upset, or very upset? Explain your answers."
)


def cloudflare_run(model, payload):
    """Send one request to the Cloudflare Workers AI REST API and return 'result'."""
    url = "https://api.cloudflare.com/client/v4/accounts/%s/ai/run/%s" % (ACCOUNT_ID, model)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + API_TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError("Cloudflare said %s: %s" % (e.code, body[:300]))
    if not data.get("success", False):
        raise RuntimeError("Cloudflare error: %s" % json.dumps(data.get("errors"))[:300])
    return data.get("result", {})


def run_chat(message):
    last_error = ""
    for model in CHAT_MODELS:
        t0 = time.time()
        try:
            r = cloudflare_run(model, {
                "messages": [
                    {"role": "system", "content": CHAT_PROMPT},
                    {"role": "user", "content": message},
                ],
                "max_tokens": 400,
            })
            text = r.get("response") or (r.get("choices") or [{}])[0].get("message", {}).get("content") or json.dumps(r)
            return 200, {"ms": int((time.time() - t0) * 1000), "model": model, "text": text}
        except Exception as e:
            last_error = str(e)
    return 502, {"error": "No chat model worked: " + last_error}


def decision_questions():
    """The same three questions are sent to every decision model."""
    return {
        "urgent": {"type": "noul", "instructions": "Is this message urgent, so that it needs action today?"},
        "team": {"type": "choice", "instructions": "Who at the school should handle this message?", "criteria": TEAMS},
        "mood": {"type": "score", "instructions": "How upset is the person who wrote this message?", "criteria": MOODS},
    }


def azure_enabled():
    return bool(AZURE_ENDPOINT and AZURE_DEPLOYMENT_NAME and AZURE_API_KEY)


def run_ms_decision(message):
    """Call Microsoft-Decision-1 on your own Azure (Foundry) deployment."""
    if not azure_enabled():
        return 400, {"error": "Microsoft-Decision-1 is not configured."}
    t0 = time.time()
    req = urllib.request.Request(
        AZURE_ENDPOINT + "/providers/microsoft/v1/systemone",
        data=json.dumps({"model": AZURE_DEPLOYMENT_NAME, "state": message, "questions": decision_questions()}).encode("utf-8"),
        headers={"api-key": AZURE_API_KEY, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return 502, {"error": "Azure said %s: %s" % (e.code, body[:300])}
    except Exception as e:
        return 502, {"error": "Azure error: " + str(e)}
    answers = data.get("answers") or (data.get("result") or {}).get("answers")
    if not answers:
        return 502, {"error": "Unexpected answer from Microsoft-Decision-1", "raw": data}
    return 200, {"ms": int((time.time() - t0) * 1000), "model": data.get("model", AZURE_DEPLOYMENT_NAME),
                 "answers": answers, "usage": data.get("usage"), "teams": TEAMS, "moods": MOODS}


def run_clef(message):
    t0 = time.time()
    try:
        r = cloudflare_run("@cf/cloudflare/" + CLEF_MODEL, {
            "model": CLEF_MODEL,
            "state": message,
            "questions": decision_questions(),
        })
        answers = r.get("answers")
        if not answers:
            return 502, {"error": "Unexpected answer from Clef", "raw": r}
        return 200, {"ms": int((time.time() - t0) * 1000), "model": CLEF_MODEL, "answers": answers, "teams": TEAMS, "moods": MOODS}
    except Exception as e:
        return 502, {"error": "Clef error: " + str(e)}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/config":
            return self.send_json(200, {"ms": azure_enabled()})
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path not in ("/api/chat", "/api/clef", "/api/ms"):
            return self.send_json(404, {"error": "Not found"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            data = {}
        message = str(data.get("message", "")).strip()[:2000]
        if not message:
            return self.send_json(400, {"error": "Write a message first"})
        handlers = {"/api/chat": run_chat, "/api/clef": run_clef, "/api/ms": run_ms_decision}
        status, result = handlers[self.path](message)
        self.send_json(status, result)

    def log_message(self, fmt, *args):
        print("  " + self.command + " " + self.path)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat AI vs Decision AI</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root {
    --ink: #1D2640; --soft: #5B6584; --paper: #F3F6FB; --card: #FFFFFF; --line: #D9E0EC;
    --chat: #6A4BE0; --chat-bg: #F0ECFF; --clef: #0B8A7A; --clef-bg: #E3F5F1; --ms: #1F6FD1; --ms-bg: #E5EFFC; --mark: #FFD23F;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--paper); color: var(--ink);
    font-family: "Atkinson Hyperlegible", system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 18px; line-height: 1.5; }
  main { max-width: 1180px; transition: max-width .2s; margin: 0 auto; padding: 32px 24px 48px; }
  h1 { font-size: clamp(30px, 4.4vw, 48px); line-height: 1.1; margin: 0 0 8px; letter-spacing: -0.01em; }
  h1 .vs { display: inline-block; background: var(--mark); padding: 0 10px; border-radius: 6px; margin: 0 6px; transform: rotate(-3deg); }
  .intro { color: var(--soft); max-width: 70ch; margin: 0 0 24px; }
  .ask { background: var(--card); border: 2px solid var(--line); border-radius: 14px; padding: 18px; }
  label { font-weight: 700; display: block; margin-bottom: 6px; }
  textarea { width: 100%; min-height: 84px; font: inherit; color: inherit; padding: 12px; border: 2px solid var(--line); border-radius: 10px; resize: vertical; }
  textarea:focus, button:focus-visible, input:focus { outline: 3px solid var(--mark); outline-offset: 2px; }
  .examples { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 14px; }
  .examples button { font: inherit; font-size: 15px; background: var(--paper); color: var(--ink); border: 1px solid var(--line); border-radius: 999px; padding: 6px 12px; cursor: pointer; }
  .examples button:hover { border-color: var(--ink); }
  .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  #run { font: inherit; font-weight: 700; font-size: 19px; background: var(--ink); color: #fff; border: 0; border-radius: 10px; padding: 12px 26px; cursor: pointer; }
  #run:disabled { opacity: .55; cursor: wait; }
  #code { font: inherit; padding: 10px; border: 2px solid var(--line); border-radius: 10px; width: 170px; display: none; }
  .lanes { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 24px; }
  .lanes.three { grid-template-columns: repeat(3, 1fr); }
  body.wide main { max-width: 1440px; }
  @media (max-width: 1000px) { .lanes, .lanes.three { grid-template-columns: 1fr; } }
  .lane { border-radius: 14px; padding: 18px 20px 22px; border: 2px solid transparent; }
  .lane.chat { background: var(--chat-bg); border-color: #DCD3FF; }
  .lane.clef { background: var(--clef-bg); border-color: #BFE7DE; }
  .lane.ms { background: var(--ms-bg); border-color: #C3D8F5; }
  .ms .clock { color: var(--ms); }
  .ms .bar .fill { background: var(--ms); }
  .lane h2 { margin: 0; font-size: 24px; }
  .lane .what { margin: 2px 0 12px; color: var(--soft); font-size: 16px; }
  .clock { font-variant-numeric: tabular-nums; font-weight: 700; font-size: clamp(40px, 6vw, 64px); line-height: 1; }
  .chat .clock { color: var(--chat); } .clef .clock { color: var(--clef); }
  .clock small { font-size: 0.4em; margin-left: 4px; }
  .stats { font-size: 15px; color: var(--soft); min-height: 22px; margin: 6px 0 12px; }
  .out { background: var(--card); border-radius: 10px; padding: 14px 16px; min-height: 120px; white-space: pre-wrap; }
  .out.empty { color: var(--soft); }
  .q { margin: 0 0 14px; } .q:last-child { margin-bottom: 0; }
  .q h3 { font-size: 17px; margin: 0 0 6px; }
  .bar { display: grid; grid-template-columns: 130px 1fr 54px; gap: 8px; align-items: center; font-size: 15px; margin: 3px 0; }
  .bar .track { height: 14px; background: #E8EDF3; border-radius: 7px; overflow: hidden; }
  .bar .fill { height: 100%; width: 0; background: var(--clef); border-radius: 7px; transition: width .6s ease; }
  .bar.top { font-weight: 700; }
  .bar .pct { text-align: right; font-variant-numeric: tabular-nums; }
  .err { color: #B3261E; white-space: normal; }
  footer { margin-top: 28px; color: var(--soft); font-size: 15px; max-width: 75ch; }
  @media (prefers-reduced-motion: reduce) { .bar .fill { transition: none; } }
</style>
</head>
<body>
<main>
  <h1>Chat AI <span class="vs">vs</span> Decision AI</h1>
  <p class="intro">Write a message someone might send to the school. The AIs read it at the same time and answer the same three questions: is it urgent, who should handle it, and how upset is the writer?</p>

  <section class="ask">
    <label for="msg">Message to the school</label>
    <textarea id="msg">The school Wi-Fi hasn't worked all morning and we can't do the online exam!</textarea>
    <div class="examples" id="examples"></div>
    <div class="row">
      <button id="run">Ask the AIs</button>
      <input id="code" placeholder="Class code" aria-label="Class code">
    </div>
  </section>

  <section class="lanes">
    <div class="lane chat">
      <h2>Chat AI</h2>
      <p class="what">Like ChatGPT: writes an answer in sentences</p>
      <div class="clock" id="chatClock">0.00<small>s</small></div>
      <div class="stats" id="chatStats"></div>
      <div class="out empty" id="chatOut">The answer will appear here.</div>
    </div>
    <div class="lane clef">
      <h2>Decision AI (Clef)</h2>
      <p class="what">Picks from fixed options and shows how sure it is</p>
      <div class="clock" id="clefClock">0.00<small>s</small></div>
      <div class="stats" id="clefStats"></div>
      <div class="out empty" id="clefOut">The decision will appear here.</div>
    </div>
    <div class="lane ms" id="msLane" style="display:none">
      <h2>Decision AI (Microsoft)</h2>
      <p class="what">Microsoft-Decision-1, same fixed options</p>
      <div class="clock" id="msClock">0.00<small>s</small></div>
      <div class="stats" id="msStats"></div>
      <div class="out empty" id="msOut">The decision will appear here.</div>
    </div>
  </section>

  <footer>All clocks start at the same moment and stop when each answer arrives. The time includes the trip over the internet, so it can change a little each time. Percentages show how sure each decision model is, not a guarantee that it is right.</footer>
</main>

<script>
var EXAMPLES = [
  ["Wi-Fi exam", "The school Wi-Fi hasn't worked all morning and we can't do the online exam!"],
  ["Hurt in PE", "My son fell during PE and his wrist is very swollen. Can someone check him?"],
  ["Trip date", "Hello, could you remind me what day the museum trip is next month? Thank you."],
  ["Cold lunch", "This is the third day in a row that the pasta at lunch was cold. Not acceptable."],
  ["Homework", "I didn't understand the maths homework for Friday, can I get some help?"],
  ["Tricky one", "The projector in room 12 smells like smoke and a student says she feels dizzy."]
];
var LABELS = { it: "IT", kitchen: "Kitchen", nurse: "Nurse", office: "Office", teacher: "Teacher" };

var ex = document.getElementById("examples");
EXAMPLES.forEach(function (e) {
  var b = document.createElement("button");
  b.type = "button"; b.textContent = e[0];
  b.onclick = function () { document.getElementById("msg").value = e[1]; };
  ex.appendChild(b);
});

function clock(id) {
  var el = document.getElementById(id), start = performance.now(), on = true;
  function tick() { if (!on) return; el.innerHTML = ((performance.now() - start) / 1000).toFixed(2) + "<small>s</small>"; requestAnimationFrame(tick); }
  tick();
  return function () { on = false; var s = (performance.now() - start) / 1000; el.innerHTML = s.toFixed(2) + "<small>s</small>"; return s; };
}

function pct(p) { return Math.round(p * 100) + "%"; }

function bar(label, p, top) {
  var row = document.createElement("div"); row.className = "bar" + (top ? " top" : "");
  var a = document.createElement("span"); a.textContent = label;
  var t = document.createElement("div"); t.className = "track";
  var f = document.createElement("div"); f.className = "fill"; t.appendChild(f);
  var c = document.createElement("span"); c.className = "pct"; c.textContent = pct(p);
  row.appendChild(a); row.appendChild(t); row.appendChild(c);
  setTimeout(function () { f.style.width = Math.max(1, p * 100) + "%"; }, 30);
  return row;
}

function question(title) {
  var q = document.createElement("div"); q.className = "q";
  var h = document.createElement("h3"); h.textContent = title; q.appendChild(h);
  return q;
}

function yesProbability(u) {
  if (!u) return 0;
  if (typeof u.noul === "number") return u.noul;
  if (typeof u.probability === "number") return u.probability;
  if (typeof u.value === "number") return u.value;
  if (u.probabilities && typeof u.probabilities["true"] === "number") return u.probabilities["true"];
  return 0;
}

function showDecision(outId, d) {
  var out = document.getElementById(outId); out.innerHTML = ""; out.classList.remove("empty");
  var a = d.answers;

  var q1 = question("Is it urgent?");
  var yes = yesProbability(a.urgent);
  q1.appendChild(bar("Yes", yes, yes >= 0.5));
  q1.appendChild(bar("No", 1 - yes, yes < 0.5));
  out.appendChild(q1);

  var q2 = question("Who should handle it?");
  var probs = (a.team && a.team.probabilities) || {};
  Object.keys(probs).sort(function (x, y) { return probs[y] - probs[x]; }).forEach(function (k, i) {
    q2.appendChild(bar(LABELS[k] || k, probs[k], i === 0));
  });
  out.appendChild(q2);

  var q3 = question("How upset is the writer?");
  var mp = (a.mood && a.mood.probabilities) || {};
  function moodP(m, i) { return mp[String(i)] || mp[m] || 0; }
  var best = -1, bestP = -1;
  (d.moods || []).forEach(function (m, i) { var p = moodP(m, i); if (p > bestP) { bestP = p; best = i; } });
  (d.moods || []).forEach(function (m, i) { q3.appendChild(bar(m, moodP(m, i), i === best)); });
  out.appendChild(q3);
}

var MS_ENABLED = false;
fetch("/api/config").then(function (r) { return r.json(); }).then(function (c) {
  if (c.ms) {
    MS_ENABLED = true;
    document.getElementById("msLane").style.display = "";
    document.querySelector(".lanes").classList.add("three");
    document.body.classList.add("wide");
  }
}).catch(function () {});

async function call(path, message) {
  var code = document.getElementById("code");
  var res = await fetch(path, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ message: message, passcode: code.value }) });
  var data = await res.json().catch(function () { return { error: "Bad response" }; });
  if (res.status === 401) { code.style.display = "inline-block"; code.focus(); }
  if (!res.ok || data.error) throw new Error(data.error || ("Error " + res.status));
  return data;
}

function showError(id, msg) {
  var out = document.getElementById(id); out.classList.remove("empty");
  out.innerHTML = ""; var p = document.createElement("div"); p.className = "err"; p.textContent = msg; out.appendChild(p);
}

document.getElementById("run").onclick = async function () {
  var message = document.getElementById("msg").value.trim();
  if (!message) return;
  var btn = this; btn.disabled = true;
  ["chatOut", "clefOut", "msOut"].forEach(function (id) { var o = document.getElementById(id); o.classList.add("empty"); o.textContent = "Thinking..."; });
  ["chatStats", "clefStats", "msStats"].forEach(function (id) { document.getElementById(id).textContent = ""; });

  var stopChat = clock("chatClock"), stopClef = clock("clefClock");

  var chatJob = call("/api/chat", message).then(function (d) {
    stopChat();
    var o = document.getElementById("chatOut"); o.classList.remove("empty"); o.textContent = d.text;
    var words = d.text.split(/\s+/).filter(Boolean).length;
    document.getElementById("chatStats").textContent = words + " words written";
  }).catch(function (e) { stopChat(); showError("chatOut", e.message); });

  var clefJob = call("/api/clef", message).then(function (d) {
    stopClef(); showDecision("clefOut", d);
    document.getElementById("clefStats").textContent = "0 words written, 3 decisions";
  }).catch(function (e) { stopClef(); showError("clefOut", e.message); });

  var jobs = [chatJob, clefJob];
  if (MS_ENABLED) {
    var stopMs = clock("msClock");
    jobs.push(call("/api/ms", message).then(function (d) {
      stopMs(); showDecision("msOut", d);
      var extra = d.usage && d.usage.prompt_tokens ? ", " + d.usage.prompt_tokens + " tokens read" : "";
      document.getElementById("msStats").textContent = "0 words written, 3 decisions" + extra;
    }).catch(function (e) { stopMs(); showError("msOut", e.message); }));
  }
  await Promise.all(jobs);
  btn.disabled = false;
};
</script>
</body>
</html>"""

if __name__ == "__main__":
    if not ACCOUNT_ID or not API_TOKEN:
        print("Missing Cloudflare details. Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN,")
        print("or write them at the top of this file.")
        raise SystemExit(1)
    print("Microsoft-Decision-1: %s" % ("enabled" if azure_enabled() else "not configured (optional)"))
    print("Demo running at http://localhost:%d  (press Ctrl+C to stop)" % PORT)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")