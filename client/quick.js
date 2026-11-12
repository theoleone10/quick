/* Quick client SDK. Include on any Quick site with:
 *   <script src="/_quick/quick.js"></script>
 * Then use the global `quick` object. No keys, no config.
 */
(function () {
  const API = "/_quick/api";

  async function call(method, path, body, headers) {
    const opts = { method, headers: headers || {} };
    if (body !== undefined) {
      if (body instanceof Blob || body instanceof ArrayBuffer) opts.body = body;
      else { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
    }
    const res = await fetch(API + path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.status + " " + res.statusText);
    return data;
  }

  function listen(url, onEvent) {
    const es = new EventSource(url);
    es.onmessage = (e) => { try { onEvent(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    return () => es.close();
  }

  const enc = encodeURIComponent;

  class Collection {
    constructor(name) { this.name = name; this.base = "/db/" + enc(name); }
    create(data) { return call("POST", this.base, data); }
    get(id) { return call("GET", this.base + "/" + enc(id)); }
    /** list({ where: {status: "draft"}, order: "-createdAt", limit: 50 }) */
    list(opts = {}) {
      const p = new URLSearchParams();
      if (opts.where) p.set("where", JSON.stringify(opts.where));
      if (opts.order) p.set("order", opts.order);
      if (opts.limit) p.set("limit", opts.limit);
      return call("GET", this.base + (p.toString() ? "?" + p : ""));
    }
    /** Merge fields into an existing document. */
    update(id, patch) { return call("PATCH", this.base + "/" + enc(id), patch); }
    /** Replace (or create) a document with a chosen id. */
    set(id, data) { return call("PUT", this.base + "/" + enc(id), data); }
    delete(id) { return call("DELETE", this.base + "/" + enc(id)); }
    /** subscribe({ onCreate(doc), onUpdate(doc), onDelete(id), onChange() }) -> unsubscribe fn */
    subscribe(h = {}) {
      return listen(API + this.base + "/events", (ev) => {
        if (ev.type === "create") h.onCreate && h.onCreate(ev.doc);
        if (ev.type === "update") h.onUpdate && h.onUpdate(ev.doc);
        if (ev.type === "delete") h.onDelete && h.onDelete(ev.id);
        if (ev.type !== "ready") h.onChange && h.onChange(ev);
      });
    }
    /** Keep a live array in sync: watch(docs => render(docs), {order, where}) */
    watch(cb, opts = {}) {
      let docs = [];
      const refresh = async () => { docs = await this.list(opts); cb(docs); };
      const stop = this.subscribe({ onChange: refresh });
      refresh();
      return stop;
    }
  }

  /** Realtime room with presence. Messages are not stored. */
  class Room {
    constructor(name) {
      this.name = name;
      this.cid = Math.random().toString(36).slice(2) + Date.now().toString(36);
      this.members = [];
      this.handlers = { message: [], join: [], leave: [], members: [] };
      this.stop = listen(`${API}/rooms/${enc(name)}/events?cid=${this.cid}`, (ev) => {
        if (ev.type === "hello") { this.members = ev.members; this._fire("members", this.members); }
        if (ev.type === "join" && ev.cid !== this.cid) {
          this.members = this.members.filter((m) => m.cid !== ev.cid).concat([{ cid: ev.cid, user: ev.user }]);
          this._fire("join", ev); this._fire("members", this.members);
        }
        if (ev.type === "leave") {
          this.members = this.members.filter((m) => m.cid !== ev.cid);
          this._fire("leave", ev); this._fire("members", this.members);
        }
        if (ev.type === "message" && ev.from !== this.cid) this._fire("message", ev.data, ev);
      });
    }
    _fire(type, ...args) { this.handlers[type].forEach((fn) => fn(...args)); }
    /** on("message" | "join" | "leave" | "members", fn) */
    on(type, fn) { this.handlers[type].push(fn); return this; }
    send(data) { return call("POST", "/rooms/" + enc(this.name), { cid: this.cid, data }); }
    leave() { this.stop(); }
  }

  let mePromise;

  window.quick = {
    site: location.hostname.split(".")[0],
    db: { collection: (name) => new Collection(name) },
    room: (name) => new Room(name),
    /** quick.me() -> { name, site } */
    me: () => (mePromise = mePromise || call("GET", "/me")),
    ai: {
      /** quick.ai.ask("Write a haiku") -> "..." */
      async ask(prompt, opts = {}) { return (await call("POST", "/ai", { ...opts, prompt })).text; },
      /** quick.ai.chat([{role:"user", content:"hi"}], {system}) -> { text, model, stop_reason } */
      chat(messages, opts = {}) { return call("POST", "/ai", { ...opts, messages }); },

      /** Stream the answer as it's written.
       *  quick.ai.stream(promptOrMessages, { system, onText(chunk, fullSoFar) }) -> { text, stop_reason } */
      async stream(input, opts = {}) {
        const { onText, ...rest } = opts;
        const body = typeof input === "string" ? { ...rest, prompt: input } : { ...rest, messages: input };
        const res = await fetch(API + "/ai", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...body, stream: true }),
        });
        if (!res.ok) {
          const d = await res.json().catch(() => ({}));
          throw new Error(d.error || res.status + " " + res.statusText);
        }
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "", text = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
            if (!chunk.startsWith("data: ")) continue; // keepalive comment
            const ev = JSON.parse(chunk.slice(6));
            if (ev.type === "text") { text += ev.text; onText && onText(ev.text, text); }
            else if (ev.type === "error") throw new Error(ev.error);
            else if (ev.type === "done") return { text, stop_reason: ev.stop_reason, model: ev.model };
          }
        }
        throw new Error("AI stream ended unexpectedly");
      },

      /** Get JSON matching a schema (structured outputs). Objects need additionalProperties:false.
       *  quick.ai.json(promptOrMessages, schema, { system }) -> parsed object */
      async json(input, schema, opts = {}) {
        const msgs = typeof input === "string" ? [{ role: "user", content: input }] : input;
        const r = await quick.ai.stream(msgs, {
          ...opts, output_config: { ...(opts.output_config || {}), format: { type: "json_schema", schema } },
        });
        if (r.stop_reason === "refusal") throw new Error("Claude declined this request");
        if (r.stop_reason === "max_tokens") throw new Error("Answer was too long; ask for fewer items");
        return JSON.parse(r.text);
      },

      /** Turn an image File into a content block, downscaled (default 1568px long side, JPEG). */
      async image(file, maxSide = 1568) {
        const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
        const s = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
        const c = document.createElement("canvas");
        c.width = Math.round(bmp.width * s); c.height = Math.round(bmp.height * s);
        const g = c.getContext("2d");
        g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
        g.drawImage(bmp, 0, 0, c.width, c.height);
        const data = c.toDataURL("image/jpeg", 0.85).split(",")[1];
        return { type: "image", source: { type: "base64", media_type: "image/jpeg", data } };
      },

      /** Turn a PDF File/Blob into a document content block. */
      async pdf(file) {
        const data = await new Promise((ok, fail) => {
          const r = new FileReader();
          r.onload = () => ok(r.result.split(",")[1]); r.onerror = fail; r.readAsDataURL(file);
        });
        return { type: "document", source: { type: "base64", media_type: "application/pdf", data } };
      },
    },
    files: {
      /** upload(fileOrBlob) -> { id, name, url, ... } */
      upload(file, name) {
        return call("POST", "/files", file, {
          "X-Filename": enc(name || file.name || "file"),
          "Content-Type": file.type || "application/octet-stream",
        });
      },
      list: () => call("GET", "/files"),
      delete: (id) => call("DELETE", "/files/" + enc(id)),
    },
  };
})();
