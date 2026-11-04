// Substituted by the installer. No credentials are embedded in this plugin.
import { spawn, spawnSync } from "node:child_process";
import { readFileSync, writeFileSync, renameSync, readdirSync, statSync, unlinkSync } from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { join } from "node:path";
const PYTHON = __PM_PYTHON__;
const HOOK = __PM_HOOK__;
const CONFIG = __PM_CONFIG__;

export const PersonalMemoryAutomatic = async ({ client, directory }) => {
  const pending = new Map();
  const contextReads = new Map();
  const turns = new Map();
  const captureInFlight = new Set();
  const call = createHookCaller();
  const text = (parts) => parts.filter(p => p.type === "text" && !p.synthetic && !p.ignored).map(p => p.text).join("\n");
  return {
    "chat.message": async (input, output) => {
      const prompt = text(output.parts);
      if (prompt) {
        contextReads.delete(input.sessionID);
        pending.set(input.sessionID, { prompt, turn_id: output.message.id, cwd: directory, workspace_root: directory });
        turns.set(input.sessionID, new Map([[output.message.id, { info: output.message, parts: new Map(output.parts.map(p => [p.id,p])) }]]));
      }
    },
    "experimental.chat.system.transform": async (input, output) => {
      if (!input.sessionID) return;
      let entry = pending.get(input.sessionID);
      if (!entry) {
        const response = await Promise.race([client.session.messages({ path: { id: input.sessionID } }),new Promise(r=>setTimeout(()=>r({}),200))]);
        const last = response.data?.filter(m => m.info.role === "user").at(-1);
        if (last) entry = { prompt: text(last.parts), turn_id: last.info.id, cwd: directory, workspace_root: directory };
      }
      entry = pending.get(input.sessionID) ?? entry;
      if (!entry?.prompt) return;
      pending.set(input.sessionID, entry);
      const readStarted = Date.now();
      // OpenCode can concurrently transform the title and the main answer for
      // one turn. Share its in-flight read: separate requests race the server's
      // inference gate and can give only the title the semantic result.
      let reading = contextReads.get(input.sessionID);
      if (!reading || reading.turn_id !== entry.turn_id) {
        reading = { turn_id: entry.turn_id, promise: call({
          hook_event_name: "context", session_id: input.sessionID,
          cwd: directory, workspace_root: directory, ...entry,
        }) };
        contextReads.set(input.sessionID, reading);
      }
      const result = await reading.promise;
      // A later user turn may have arrived while this read was in flight.
      // Never inject the older result or overwrite that turn's capture origin.
      if (pending.get(input.sessionID)?.turn_id !== entry.turn_id) return;
      if (result.scope) entry.origin_project_id = result.scope.project_id;
      pending.set(input.sessionID, entry);
      if (result.context_text) output.system.push(result.context_text);
      try {
        const state = JSON.parse(readFileSync(CONFIG, "utf8")).state_dir;
        const target = join(state, "read-receipt.json");
        const receipt = { at: Date.now(), elapsed_ms: Date.now()-readStarted,
          session_hash: createHash("sha256").update(input.sessionID).digest("hex"),
          turn_hash: createHash("sha256").update(entry.turn_id).digest("hex"),
          outcome: result.read_outcome?.outcome ?? (result.context_text ? "read" : "empty_or_disabled"),
          read_outcome: result.read_outcome,
          trace_id: result.trace_id, matches: result.items?.map(m=>({memory_id:m.memory_id,version:m.version})),
          stage: result.context_text ? "context_ready" : "no_context", note: "Injection is not proof of final-answer use" };
        for (const destination of [target, join(state, "read-"+receipt.session_hash+".json")]) {
          const temporary = destination + "." + randomUUID() + ".tmp";
          writeFileSync(temporary, JSON.stringify(receipt));
          renameSync(temporary, destination);
        }
        const receipts = readdirSync(state).filter(n=>/^read-[a-f0-9]{64}\.json$/.test(n))
          .map(n=>({path:join(state,n),at:statSync(join(state,n)).mtimeMs})).sort((a,b)=>b.at-a.at);
        for (const old of receipts.slice(128)) unlinkSync(old.path);
      } catch { /* Optional timing telemetry must not affect normal work. */ }
    },
    event: async ({ event }) => {
      const sid=event.properties.sessionID;
      const cache=turns.get(sid);
      if (cache && event.type === "message.updated") {
        const info=event.properties.info;
        const current=cache.get(info.id)??{parts:new Map()};current.info=info;cache.set(info.id,current);
      }
      if (cache && event.type === "message.part.updated") {
        const part=event.properties.part;
        const current=cache.get(part.messageID)??{parts:new Map()};current.parts.set(part.id,part);cache.set(part.messageID,current);
      }
      if (cache && event.type === "message.part.delta") {
        const p=event.properties;const part=cache.get(p.messageID)?.parts.get(p.partID);
        if(part && p.field==="text")part.text=(part.text??"")+p.delta;
      }
      if (event.type !== "session.idle" && !(event.type === "session.status" && event.properties.status?.type === "idle")) return;
      const session = event.properties.sessionID;
      if (captureInFlight.has(session)) return;
      captureInFlight.add(session);
      try {
        // Capture from events already delivered by the real host. Calling the
        // SDK here races CLI server disposal and can lose the completed turn.
        const messages = [...(turns.get(session)?.values()??[])].filter(m=>m.info).map(m=>({info:m.info,parts:[...m.parts.values()]}));
        const user = messages.filter(m => m.info.role === "user").at(-1);
        if (!user) return;
        const assistants = messages.filter(m => m.info.role === "assistant" && m.info.parentID === user.info.id);
        const final = assistants.at(-1);
        if (!final?.info.time?.completed || final.info.finish === "tool-calls") return;
        const values = [user, ...assistants].map(m => ({ id: m.info.id, role: m.info.role, text: text(m.parts) })).filter(m => m.text);
        for(const message of assistants)for(const part of message.parts){
          if(part.type!=="tool" || !["completed","error"].includes(part.state?.status) || (part.tool??"").includes("personal_memory"))continue;
          const raw=part.state.output??part.state.error??"";
          values.push({id:message.info.id+":tool:"+part.id,role:"tool",text:"工具 "+part.tool+" 执行观察：\n"+(typeof raw==="string"?raw:JSON.stringify(raw))});
        }
        const origin = pending.get(session);
        const result = await call({ hook_event_name: "capture", session_id: session, turn_id: user.info.id,
          cwd: origin?.cwd ?? null, workspace_root: origin?.workspace_root ?? null,
          capture_version: 2, origin_project_id: origin?.origin_project_id ?? null,
          completion_state: final.info.error || final.info.finish === "length" ? "incomplete" : "completed", messages: values });
        if (!result.durable) return;
        pending.delete(session);
        contextReads.delete(session);
        turns.delete(session);
      } catch {
        // Host SDK/network failures do not stop the user's work. The runtime's
        // status and durable outbox report captures that actually succeeded.
      } finally { captureInFlight.delete(session); }
    },
  };
};

// V1.19 accepts server; V2 requires setup. The two lifecycles share only the
// bounded, durable Python transport. Neither adds memory to persisted input.
export default { id: "personal-memory-automatic", server: PersonalMemoryAutomatic,
  async setup(ctx) {
    const call = createHookCaller();
    const sessions = new Map();
    const controller = new AbortController();
    let timer, closed = false, scanning = false, dirty = false;
    const bounded = async (operation) => {
      let timeout;
      try { return await Promise.race([operation, new Promise(resolve => {
        timeout = setTimeout(() => resolve(undefined), 500);
      })]); } finally { clearTimeout(timeout); }
    };
    const delivered = async (sid) => {
      const messages = await bounded(ctx.session.context({ sessionID: sid }));
      return Array.isArray(messages) ? messages : undefined;
    };
    const directory = async (sid) => {
      const info = await bounded(ctx.session.get({ sessionID: sid }));
      // A global plugin's ctx.location is not necessarily this session's root.
      return info?.location?.directory;
    };
    const entriesFor = sid => {
      if (!sessions.has(sid)) sessions.set(sid, new Map());
      return sessions.get(sid);
    };
    const capture = async () => {
      if (scanning || closed) { dirty = true; return; }
      scanning = true;
      try {
        for (const [sid, entries] of sessions) {
          const messages = await delivered(sid);
          if (!messages) continue;
          for (const [id, entry] of entries) {
            const start = messages.findIndex(m => m.type === "user" && m.id === id);
            if (start < 0 || !entry.cwd) continue;
            const next = messages.findIndex((m, index) => index > start && m.type === "user");
            const block = messages.slice(start, next < 0 ? undefined : next);
            const assistants = block.filter(m => m.type === "assistant");
            const final = assistants.at(-1);
            if (!final?.time?.completed || !final.finish || final.finish === "tool-calls") continue;
            const values = [{ id, role: "user", text: messages[start].text }];
            for (const message of assistants) {
              const text = (message.content ?? []).filter(p => p.type === "text").map(p => p.text).join("\n");
              if (text) values.push({ id: message.id, role: "assistant", text });
              for (const part of message.content ?? []) {
                if (part.type !== "tool" || !["completed", "error"].includes(part.state?.status)) continue;
                if ((part.name ?? "").includes("personal_memory")) continue;
                const raw = (part.state.content ?? []).filter(p => p.type === "text").map(p => p.text).join("\n") || part.state.error;
                if (raw) values.push({ id: message.id + ":tool:" + part.id, role: "tool",
                  text: "工具 " + part.name + " 执行观察：\n" + (typeof raw === "string" ? raw : JSON.stringify(raw)) });
              }
            }
            const result = await call({ hook_event_name: "capture", session_id: sid,
              turn_id: id, cwd: entry.cwd, workspace_root: entry.cwd,
              capture_version: 2, origin_project_id: entry.origin_project_id ?? null,
              completion_state: final.finish === "stop" && !final.error ? "completed" : "incomplete",
              messages: values });
            if (result.durable) entries.delete(id);
          }
          if (!entries.size) sessions.delete(sid);
        }
      } catch { /* An unavailable host must not interrupt the user's session. */ }
      finally {
        scanning = false;
        if (dirty && !closed) { dirty = false; schedule(); }
      }
    };
    const schedule = () => {
      if (closed || timer) return;
      timer = setTimeout(() => { timer = undefined; void capture(); }, 100);
    };
    await ctx.session.hook("prompt", async event => {
      // Admission may be retried. The durable host message ID is also the
      // backend idempotency identity, never a new random per-callback ID.
      if (!event.sessionID || !event.messageID || !event.prompt?.text) return;
      const cwd = await directory(event.sessionID);
      const entries = entriesFor(event.sessionID);
      if (!entries.has(event.messageID)) entries.set(event.messageID, {
        prompt: event.prompt.text, turn_id: event.messageID, cwd,
      });
    });
    await ctx.session.hook("context", async event => {
      const sid = event.sessionID;
      if (!sid) return;
      const messages = await delivered(sid);
      const user = messages?.filter(m => m.type === "user").at(-1);
      if (!user?.id || !user.text) return;
      const entries = entriesFor(sid);
      let entry = entries.get(user.id);
      if (!entry) {
        entry = { prompt: user.text, turn_id: user.id, cwd: await directory(sid) };
        entries.set(user.id, entry);
      }
      if (!entry.cwd) return;
      entry.read ??= call({ hook_event_name: "context", session_id: sid,
        prompt: user.text, turn_id: user.id, cwd: entry.cwd, workspace_root: entry.cwd });
      const result = await entry.read;
      if (result.scope) entry.origin_project_id = result.scope.project_id;
      if (result.context_text) event.system.push({ type: "text", text: result.context_text });
      schedule();
    });
    // V2 emits different event names. Inspect only sessions this plugin has
    // observed, and use their projected typed messages as completion evidence.
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          if (sessions.size) schedule();
        }
      } catch { /* Abort and transport closure are normal during reload. */ }
    })();
    return () => { closed = true; clearTimeout(timer); controller.abort(); };
  }
};

function createHookCaller() {
  return (event) => {
    if(event.hook_event_name==="capture"){
      // Only the tiny local outbox write is synchronous. Otherwise the CLI can
      // dispose its process tree before a detached Python hook even starts.
      try{
        const r=spawnSync(PYTHON,["-X","utf8",HOOK,"--config",CONFIG],{input:JSON.stringify(event),encoding:"utf8",windowsHide:true,timeout:900,maxBuffer:128000});
        return Promise.resolve(JSON.parse(r.stdout||"{}"));
      }catch{return Promise.resolve({});}
    }
    return new Promise((resolve) => {
    const child = spawn(PYTHON, ["-X", "utf8", HOOK, "--config", CONFIG], {
      windowsHide: true, stdio: ["pipe", "pipe", "ignore"],
    });
    let stdout = "";
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };
    const timer = setTimeout(() => {
      child.kill();
      finish({read_outcome:{outcome:"process_timeout", stdout_bytes:Buffer.byteLength(stdout)}});
    }, 1400);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (data) => {
      if (settled) return;
      if (stdout.length + data.length > 128000) {
        child.kill(); finish({read_outcome:{outcome:"protocol_overflow"}}); return;
      }
      stdout += data;
      // stdout is one JSON line. A completed response must not be lost while
      // the Windows launcher is still waiting for the Python child to exit.
      if (stdout.includes("\n")) {
        try {
          const result = JSON.parse(stdout);
          if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error();
          finish(result);
        } catch { finish({read_outcome:{outcome:"protocol_error"}}); }
      }
    });
    child.on("error", () => { finish({read_outcome:{outcome:"spawn_error"}}); });
    child.on("close", () => {
      clearTimeout(timer);
      if (settled) return;
      try {
        const result = JSON.parse(stdout);
        if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error();
        finish(result);
      } catch { finish({read_outcome:{outcome:"protocol_error"}}); }
    });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(event));
    });
  };
}
