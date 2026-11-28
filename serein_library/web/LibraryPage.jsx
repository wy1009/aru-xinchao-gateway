import { useEffect, useRef, useState } from "react";
import { MarkdownProjection } from "../components/MarkdownProjection.jsx";
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

const diffFields = { title: "标题", content: "正文", summary: "摘要" };
const historyDate = new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" });

function Difference({ change }) {
  const { before, after } = change;
  const chars = text => Array.from(text);
  const old = chars(before), current = chars(after);
  let start = 0, end = 0;
  while (start < old.length && start < current.length && old[start] === current[start]) start++;
  while (end < old.length - start && end < current.length - start &&
    old[old.length - 1 - end] === current[current.length - 1 - end]) end++;
  function line(value, label, className) {
    if (!value.length) return null;
    const stop = value.length - end;
    const changed = value.slice(start, stop).join("");
    const visible = changed && !changed.trim()
      ? changed.replaceAll("\r", "␍").replaceAll("\n", "↵\n").replaceAll("\t", "⇥").replaceAll(" ", "·")
      : changed;
    return <div className={className}><small>{label}</small><pre>{value.slice(0, start).join("")}
      {changed && <mark>{visible}</mark>}{value.slice(stop).join("")}</pre></div>;
  }
  const label = diffFields[change.field];
  const location = change.field === "content" ? " · 第 " + (after ? change.new_line : change.old_line) + " 行" : "";
  return <section className="library-difference"><h4>{label}{location}</h4>
    {line(old, "历史版本", "library-before")}{line(current, "当前版本", "library-after")}
  </section>;
}

function HistoryPanel({ data, busy, onSelect, onRestore }) {
  return <section className="library-history" aria-label="历史版本"><h3>历史版本</h3>
    {data.versions.length === 0 ? <p>还没有旧版本。下次修改时，会自动保留这一版。</p> : <>
      <label>对比版本<select value={data.selected_revision} disabled={busy}
        onChange={e => onSelect(Number(e.target.value))}>
        {data.versions.map(v => <option key={v.revision} value={v.revision}>
          版本 {v.revision} · {historyDate.format(new Date(v.replaced_at))} 归档
        </option>)}</select></label>
      <p>版本 {data.selected_revision} 与当前版本 {data.current_revision} 的差异</p>
      {data.changes.length ? <>
        {data.changes.map((change,i) => <Difference key={i} change={change}/>)}
        <button disabled={busy} onClick={onRestore}>回退到版本 {data.selected_revision}</button>
      </> : <p>两版内容相同。</p>}
    </>}
  </section>;
}

export function LibraryPage() {
  const [catalog, setCatalog] = useState([]);
  const [group, setGroup] = useState("信息卡");
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("");
  const [results, setResults] = useState(null);
  const [doc, setDoc] = useState(null);
  const [history, setHistory] = useState(null);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const pending = useRef(0);
  const heading = useRef(null);
  const searchInput = useRef(null);
  useEffect(() => {
    let active = true;
    request("materials").then(data => { if (active) setCatalog(data.items); })
      .catch(err => { if (active) setNotice(err.message); });
    return () => { active = false; pending.current++; };
  }, []);
  function leaveEditor() {
    if (draft && (draft.title !== (doc?.title || "") || draft.content !== (doc?.content || "")) &&
        !window.confirm("放弃尚未保存的改动？")) return false;
    setDraft(null); setHistory(null);
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
    if (busy || !query.trim() || !leaveEditor()) return;
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
  async function showSaved(id, message) {
    const latest = await request("materials/" + id);
    setDoc(latest); setDraft(null); setHistory(null); setGroup(latest.kind); setResults(null);
    setCatalog((await request("materials")).items);
    setNotice(message);
  }
  async function showHistory(revision) {
    setBusy(true); setNotice("");
    try {
      const path = "materials/" + doc.id + "/history";
      setHistory(await request(revision ? path + "/" + revision : path));
    } catch (err) { setNotice(err.message); }
    finally { setBusy(false); }
  }
  async function restore() {
    const target = history.selected_revision;
    if (!window.confirm(`将「${history.current_title}」回退到版本 ${target}？当前版本会保留在历史中。`)) return;
    setBusy(true); setNotice("");
    try {
      const saved = await request(`materials/${doc.id}/history/${target}`, "POST",
        { expected_revision: history.current_revision });
      await showSaved(saved.id, `已回退到版本 ${target} 的内容，当前为版本 ${saved.revision}。`);
    } catch (err) { setNotice(err.message); }
    finally { setBusy(false); }
  }
  async function save(e) {
    e.preventDefault();
    setBusy(true); setNotice("");
    try {
      const saved = doc ? await request("materials/" + doc.id, "PATCH",
        { expected_revision:doc.revision, title:draft.title, content:draft.content })
        : await request("materials", "POST", draft);
      await showSaved(saved.id, "已保存");
    } catch (err) { setNotice(err.message); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!window.confirm(`删除${doc.kind}「${doc.title}」？删除后，资料目录、搜索和自动召回都会移除这份资料。`)) return;
    setBusy(true); setNotice("");
    try {
      const result = await request("materials/" + doc.id, "DELETE", { expected_revision: doc.revision });
      const remaining = items => items.filter(item => item.id !== doc.id);
      setCatalog(remaining);
      setResults(items => items === null ? null : remaining(items));
      setDoc(null);
      setNotice(result.deleted ? `已删除${doc.kind}「${doc.title}」` : "这份资料已被删除。");
      searchInput.current?.focus();
    } catch (err) { setNotice(err.message); }
    finally { setBusy(false); }
  }
  const shown = results ?? catalog.filter(item => item.kind === group);
  return <div className="library-page">
    <header><h1>资料</h1></header>
    <form className="library-search" onSubmit={lookup}>
      <input ref={searchInput} aria-label="搜索事件、原文和资料" placeholder="搜索事件、原文和资料" value={query}
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
      </form> : doc && <><header className="library-reader-header">
        <div><small>{doc.kind}{doc.revision && ' · 版本 '+(history?.current_revision ?? doc.revision)}</small>
          <h2 ref={heading} tabIndex={-1}>{history?.current_title ?? doc.title}</h2></div>
        {categories.includes(doc.kind) && <div className="library-actions">
          {history ? <button disabled={busy} onClick={() => open(doc)}>返回正文</button> : <>
            <button disabled={busy} onClick={() => setDraft({...doc})}>编辑</button>
            <button disabled={busy} onClick={() => showHistory()}>历史版本</button>
            <button className="library-delete" disabled={busy} onClick={remove}>删除</button>
          </>}
        </div>}
      </header>
        {history ? <HistoryPanel data={history} busy={busy} onSelect={showHistory} onRestore={restore}/>
          : doc.kind === "原文"
          ? <div className="library-body library-original">{doc.content}</div>
          : <MarkdownProjection className="library-body" content={doc.content}/>}
      </>}
    </article></div>
  </div>;
}
