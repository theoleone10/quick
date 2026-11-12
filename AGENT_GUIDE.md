# This folder is a Quick site

Quick hosts plain static files: HTML, CSS, JS and assets. There's no build step and no server code. Deploy with:

```
python <path-to-quick>/quick.py deploy . <site-name>
```

The site goes live at `http://<site-name>.localhost:8000/`. Deploying again replaces every file.

## Backend APIs (browser only)

Add `<script src="/_quick/quick.js"></script>` to the page. It defines a global `quick` object, and no keys or config are needed. All data is scoped to this site.

### Database: JSON document collections, no schema

```js
const todos = quick.db.collection("todos");
const t = await todos.create({ text: "hi", done: false }); // -> { id, text, done, createdAt, updatedAt, createdBy }
await todos.get(t.id);
await todos.list({ where: { done: false }, order: "-createdAt", limit: 50 }); // where = exact-match only
await todos.update(t.id, { done: true });   // merge fields
await todos.set("settings", { theme: "dark" }); // replace or create with a chosen id
await todos.delete(t.id);
const stop = todos.subscribe({ onCreate(doc) {}, onUpdate(doc) {}, onDelete(id) {} });
const stop2 = todos.watch(docs => render(docs), { order: "-createdAt" }); // live array; easiest option
```

`createdAt` and `updatedAt` are unix seconds, and `createdBy` is the viewer's name. Ids match `[A-Za-z0-9_-]{1,64}`.

### Realtime rooms: ephemeral messages plus presence, nothing stored

```js
const room = quick.room("lobby");
room.on("message", data => {});             // messages from others (not your own)
room.on("members", list => {});             // [{cid, user}], fires on every join/leave
room.send({ x: 10, y: 20 });                // e.g. cursors, typing indicators, game moves
```

### AI: Claude, with the server holding the key

```js
const text = await quick.ai.ask("Summarise: ...", { system: "Be brief" });
const { text: reply } = await quick.ai.chat([{ role: "user", content: "hi" }], { system: "..." });
```

Calls take a few seconds, so show a loading state.

### Files

```js
const f = await quick.files.upload(inputEl.files[0]); // -> { id, name, url, size, mime }
img.src = f.url;
await quick.files.list(); await quick.files.delete(f.id);
```

### Identity

```js
const me = await quick.me(); // -> { name, site }
```

## Rules of thumb

- Keep it to static files. Inline CSS and JS are fine, and so are CDN imports.
- Use `textContent` (not `innerHTML`) for anything that came from the database or from other users.
- Each `subscribe`/`watch`/`room` holds one open connection. Browsers allow about 6 per site, so reuse them.
- Writes are rate-limited to about 20/sec per user. Don't write in tight loops; throttle cursor sends to about 15/sec.
