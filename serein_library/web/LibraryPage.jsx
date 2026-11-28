import { useEffect, useRef, useState } from "react";
import "./library.css";

const categories = ["信息卡", "长期资料"];
const endpoint = "/__serein/library-api/";

async function request(path, method = "GET", payload) {
  const response = await fetch(endpoint + path, {
    method, headers: payload ? { "Content-Type": "application/json" } : undefined,
    body: payload ? JSON.stringify(payload) : undefined,
  });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "资料请求失败");
  return value;
}

export function LibraryPage() {
  const [catalog, setCatalog] = useState([]);
  const [group, setGroup] = useState("信息卡");
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("");
  const [results, setResults] = useState(null);
  const [doc, setDoc] = useState(null);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const pending = useRef(0);
  const heading = useRef(null);
  useEffect(() => {
    let active = true;
    request("materials").then(data => { if (active) setCatalog(data.items); })
      .catch(err => { if (active) setNotice(err.message); });
    return () => { active = false; pending.current++; };
  }, []);
  function leaveEditor() {
    if (draft && (draft.title !== (doc?.title || "") || draft.content !== (doc?.content || "")) &&
        !window.confirm("放弃尚未保存的改动？")) return false;
    setDraft(null);
    return true;
  }
  function showGroup(value) {
    if (!leaveEditor()) return;
    pending.current++;
    setBusy(false); setGroup(value); setResults(null); setDoc(null); setNotice("");
  }
  async function open(item) {
    if (!leaveEditor()) return;
    const version = ++pending.current;
    setBusy(true); setNotice("");
    try {
      const next = categories.includes(item.kind) ? await request("materials/" + item.id)
        : await request("source", "POST", { id: item.id });
      if (version === pending.current) {
        setDoc(next);
        requestAnimationFrame(() => heading.current?.focus());
      }
    } catch (err) { if (version === pending.current) setNotice(err.message); }
    finally { if (version === pending.current) setBusy(false); }
  }
  async function lookup(e) {
    e.preventDefault();
    if (!query.trim() || !leaveEditor()) return;
    const version = ++pending.current;
    setBusy(true); setNotice("");
    try {
      const data = await request("search", "POST", { query: query.trim(), kind:scope || null });
      if (version === pending.current) {
        setResults(data.items); setDoc(null); setNotice(data.warnings.join(" · "));
      }
    } catch (err) { if (version === pending.current) setNotice(err.message); }
    finally { if (version === pending.current) setBusy(false); }
  }
  function startNew() {
    if (!leaveEditor()) return;
    setDoc(null); setNotice("");
    setDraft({ kind:group, title:"", content:"", request_id:crypto.randomUUID() });
  }
  async function save(e) {
    e.preventDefault();
    setBusy(true); setNotice("");
    try {
      const saved = doc ? await request("materials/" + doc.id, "PATCH",
        { expected_revision:doc.revision, title:draft.title, content:draft.content })
        : await request("materials", "POST", draft);
      const latest = await request("materials/" + saved.id);
      setDoc(latest); setDraft(null); setGroup(latest.kind); setResults(null);
      setCatalog((await request("materials")).items);
      setNotice("已保存");
    } catch (err) { setNotice(err.message); }
    finally { setBusy(false); }
  }
  const shown = results ?? catalog.filter(item => item.kind === group);
  return <div className="library-page">
    <header><h1>资料</h1></header>
    <form className="library-search" onSubmit={lookup}>
      <input aria-label="搜索事件、原文和资料" placeholder="搜索事件、原文和资料" value={query}
        onChange={e => setQuery(e.target.value)} />
      <select aria-label="搜索范围" value={scope} onChange={e => setScope(e.target.value)}>
        <option value="">全部类型</option>{[...categories,"事件","原文"].map(k => <option key={k}>{k}</option>)}
      </select>
      <button disabled={busy || !query.trim()}>搜索</button>
    </form>
    <nav aria-label="资料分类">{categories.map(kind =>
      <button key={kind} onClick={() => showGroup(kind)} disabled={busy}
        aria-pressed={results === null && group === kind}>{kind}（{catalog.filter(x => x.kind === kind).length}）</button>)}
      <button className="library-new" onClick={startNew} disabled={busy}>新建{group}</button></nav>
    <p role="status">{busy ? "正在处理…" : notice}</p>
    <div className="library-columns"><div className="library-list">
      {results && <h2>搜索结果</h2>}
      {shown.length === 0 && <p>{results ? "没有找到匹配的内容。" : "暂无资料。"}</p>}
      {shown.map(row => <button key={row.id} disabled={busy} onClick={() => open(row)}
        aria-pressed={doc?.id === row.id}>{results && <small>{row.kind}</small>}
        <strong>{row.title}</strong>{results && <span>{row.passage.slice(0,140)}</span>}</button>)}
    </div>
    <article className="library-detail">
      {draft ? <form onSubmit={save}>
        <label>标题<input aria-label="标题" required maxLength={240} value={draft.title}
          onChange={e => setDraft({...draft,title:e.target.value})}/></label>
        <label>正文<textarea aria-label="正文" required rows={16} value={draft.content}
          onChange={e => setDraft({...draft,content:e.target.value})}/></label>
        <button disabled={busy}>保存</button> <button type="button" disabled={busy} onClick={leaveEditor}>取消</button>
      </form> : doc && <><small>{doc.kind}{doc.revision && ' · 版本 '+doc.revision}</small>
        <h2 ref={heading} tabIndex={-1}>{doc.title}</h2>
        {categories.includes(doc.kind) && <button disabled={busy} onClick={() => setDraft({...doc})}>编辑</button>}
        <div className="library-body">{doc.content}</div></>}
    </article></div>
  </div>;
}
