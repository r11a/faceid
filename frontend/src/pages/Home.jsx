import React, { useEffect, useRef, useState } from "react";
import { ArrowLeft, Camera, ChevronLeft, ChevronRight, MapPin, Play, Search, UserPlus, UserRound, Users, X } from "lucide-react";
import { api, assetUrl, query } from "../api.js";
import { useResource } from "../hooks.js";
import { Empty, ErrorState, Loading, Modal, dateTime, relativeTime } from "../ui.jsx";
import { chronological, clipAfter, eventTime, identityForEvent, latestPeople } from "../home.js";
import { PersonEditor } from "./Daily.jsx";
import { EventVideo } from "../EventVideo.jsx";

function Photo({ src, name = "פנים", className = "" }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  return <span className={`home-photo ${className}`}>{src && !failed ? <img src={assetUrl(src)} alt={name} loading="lazy" onError={() => setFailed(true)} /> : <UserRound />}</span>;
}

function Rail({ label, children, resetKey, className = "" }) {
  const ref = useRef(null);
  const drag = useRef(null);
  const suppressClick = useRef(false);
  useEffect(() => { if (ref.current) ref.current.scrollLeft = 0; }, [resetKey]);
  const move = direction => ref.current?.scrollBy({ left: direction * ref.current.clientWidth * .8, behavior: "smooth" });
  return <div className={`home-rail-wrap ${className}`}>
    <div className="home-rail-controls"><button className="icon-button" aria-label={`${label}: ימינה`} onClick={() => move(1)}><ChevronRight /></button><button className="icon-button" aria-label={`${label}: שמאלה`} onClick={() => move(-1)}><ChevronLeft /></button></div>
    <div ref={ref} className="home-rail" role="region" aria-label={label} tabIndex={0}
      onDragStart={event => event.preventDefault()}
      onPointerDown={event => { if (event.pointerType === "mouse" && event.button === 0) { drag.current = { x: event.clientX, left: ref.current.scrollLeft }; suppressClick.current = false; } }}
      onPointerMove={event => { if (!drag.current || !(event.buttons & 1)) return; const dx = event.clientX - drag.current.x; if (Math.abs(dx) > 6) { suppressClick.current = true; ref.current.scrollLeft = drag.current.left - dx; } }}
      onPointerUp={() => { drag.current = null; }} onPointerLeave={() => { drag.current = null; }}
      onClickCapture={event => { if (suppressClick.current) { event.preventDefault(); event.stopPropagation(); suppressClick.current = false; } }}>
      {children}
    </div>
  </div>;
}

function useAppearances(selection, page, retry) {
  const sourceKey = JSON.stringify(selection?.source || null);
  const state = useResource(`faces/timeline?${query({ ...JSON.parse(sourceKey), order: "desc", limit: 60, offset: page * 60 })}`, {
    enabled: sourceKey !== "null", realtime: true, poll: 15000,
  });
  useEffect(() => { if (retry) state.reload(); }, [retry, state.reload]);
  return state;
}

export function ClipSequence({ events, people = [] }) {
  const [sequence, setSequence] = useState(null);
  const rows = sequence || chronological(events);
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [finished, setFinished] = useState(false);
  const [unavailable, setUnavailable] = useState(0);
  const current = rows[index];
  const advance = (failed = false) => {
    if (failed) setUnavailable(value => value + 1);
    const next = clipAfter(index, rows.length);
    if (next === null) { setPlaying(false); setFinished(true); }
    else setIndex(next);
  };
  if (!rows.length) return null;
  return <div className="home-sequence">
    <div className="home-video-stage">{playing ? <EventVideo eventId={current.event_id} nextEventId={rows[index + 1]?.event_id} onEnded={() => advance(false)} onUnavailable={() => advance(true)} className={identityForEvent(current, people)?.watchlist === "black" ? "blacklist-outline" : ""} /> : <>
      <Photo src={current.image || `api/activity/${encodeURIComponent(current.event_id)}/image`} name="תמונת האירוע" />
      <button className="button primary" onClick={() => { setSequence(chronological(events)); setIndex(0); setUnavailable(0); setFinished(false); setPlaying(true); }}><Play />{finished ? "נגן שוב" : "נגן רצף"}</button>
    </>}</div>
    <div className="home-video-caption"><b><Camera />{current.camera || "מצלמה לא ידועה"}</b><span>{dateTime(eventTime(current))}</span><span>{index + 1} / {rows.length}</span></div>
    {unavailable > 0 && <p className="home-muted" role="status">{unavailable} קליפים לא זמינים — דולגו</p>}
    {finished && <p role="status">הרצף הסתיים</p>}
    <ol className="home-route">{rows.map((row, i) => <li key={row.event_id}><button className={i === index ? "active" : ""} aria-current={i === index ? "step" : undefined} onClick={() => { setSequence(rows); setIndex(i); setPlaying(true); setFinished(false); }}><span>{i + 1}</span><b>{row.camera || "—"}</b><time>{dateTime(eventTime(row))}</time><Play /></button></li>)}</ol>
  </div>;
}

function HistoryPages({ page, count, total, onChange }) {
  return <div className="home-history-pagination"><button className="text-button" disabled={(page + 1) * 60 >= total} onClick={() => onChange(page + 1)}>אירועים קודמים</button><span>{count ? `${page * 60 + 1}–${page * 60 + count} / ${total}` : "0"}</span><button className="text-button" disabled={!page} onClick={() => onChange(page - 1)}>אירועים חדשים יותר</button></div>;
}

export function DashboardPage({ navigate }) {
  const dashboard = useResource("dashboard", { poll: 15000, realtime: true });
  const [selection, setSelection] = useState(null);
  const [page, setPage] = useState(0);
  const [retry, setRetry] = useState(0);
  const [editor, setEditor] = useState(false);
  const [picker, setPicker] = useState(false);
  const [details, setDetails] = useState(false);
  const [playbackEvent, setPlaybackEvent] = useState(null);
  const [candidates, setCandidates] = useState(null);
  const [resolving, setResolving] = useState(false);
  const [selectionError, setSelectionError] = useState(null);
  const request = useRef(null);
  const appearances = useAppearances(selection, page, retry);
  useEffect(() => () => request.current?.abort(), []);
  const select = value => {
    request.current?.abort(); setResolving(false); setSelectionError(null); setCandidates(null);
    setSelection(value); setPage(0); setDetails(false);
  };
  const people = latestPeople(dashboard.data?.people || []);
  const selectPerson = person => { select({ name: person.name, photo: person.photo, person, source: { person: person.slug } }); setDetails(true); };
  const selectEvent = async event => {
    const person = identityForEvent(event, people);
    if (person) { select({ name: person.name, photo: person.photo, person, source: { person: person.slug } }); return; }
    const choice = { name: "לא מזוהה", photo: event.image || `api/activity/${encodeURIComponent(event.event_id)}/image`, source: { event_id: event.event_id } };
    if (event.sample_id) { select({ ...choice, source: { sample_id: event.sample_id } }); return; }
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setResolving(true); setSelectionError(null);
    try {
      const data = await api(`faces/event/${encodeURIComponent(event.event_id)}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      if (data.samples.length > 1) { setCandidates(data.samples); return; }
      select(data.samples.length ? { ...choice, source: { sample_id: data.samples[0].sample_id } } : choice);
    } catch (error) { if (!controller.signal.aborted) setSelectionError(error); }
    finally { if (!controller.signal.aborted) setResolving(false); }
  };
  const rows = chronological(selection ? appearances.data?.events || [] : dashboard.data?.recent || []);
  const currentSource = JSON.stringify(selection?.source);
  const loading = selection && appearances.loading;
  const error = selection ? appearances.error : null;
  const showDetails = () => setDetails(true);
  const openEvent = event => { setPlaybackEvent(event); if (!selection) selectEvent(event); };
  if (dashboard.loading) return <Loading />;
  if (dashboard.error) return <ErrorState error={dashboard.error} retry={dashboard.reload} />;
  return <div className="home-workspace">
    <header className="home-heading"><h1>מבט כללי</h1><div><button className="button secondary" onClick={() => setPicker(true)}><Search />לא מזוהים</button><button className="button primary" onClick={() => setEditor(true)}><UserPlus />הוסף אדם</button></div></header>
    <section className="home-section" aria-labelledby="home-events-title">
      <div className="home-section-heading"><div><h2 id="home-events-title">{selection ? `ההופעות של ${selection.name}` : "אירועים אחרונים"}</h2>{selection && !selection.person && <span className="home-muted">התאמות דומות · זהות לא מאומתת</span>}</div>
        <div className="home-section-actions">{selection ? <><button className="text-button" onClick={showDetails}><Play />פרטים ורצף</button><button className="button secondary" onClick={() => select(null)}><X />כולם</button></> : <button className="text-button" onClick={() => navigate("events")}>כל האירועים <ArrowLeft /></button>}</div>
      </div>
      {resolving && <Loading text="מאתר פנים…" />}
      {selectionError && <ErrorState error={selectionError} />}
      {loading ? <Loading /> : error ? <ErrorState error={error} retry={() => setRetry(v => v + 1)} /> : rows.length ? <Rail label="אירועים" resetKey={`${currentSource}-${page}`} className="home-event-rail">
        {rows.map(event => { const person = identityForEvent(event, people); return <button className={`home-event-card ${person?.watchlist === "black" ? "blacklist-outline" : ""}`} key={event.event_id} onClick={() => openEvent(event)}>
          <Photo src={event.image || `api/activity/${encodeURIComponent(event.event_id)}/image`} name={selection?.name || person?.name || "לא מזוהה"} />
          <div><b>{selection?.name || person?.name || "לא מזוהה"}</b><span><Camera />{event.camera || "—"}</span><time>{dateTime(eventTime(event))}</time></div>
        </button>; })}
      </Rail> : <Empty icon={Camera} title={selection ? "לא נמצאו הופעות" : "אין אירועים עדיין"} />}
      {selection && !loading && !error && <HistoryPages page={page} count={rows.length} total={appearances.data?.total || 0} onChange={setPage} />}
    </section>
    <section className="home-section" aria-labelledby="home-people-title"><div className="home-section-heading"><h2 id="home-people-title">מיקום אחרון</h2><button className="text-button" onClick={() => navigate("people")}>כל האנשים <ArrowLeft /></button></div>
      {people.length ? <Rail label="אנשים"><>{people.map(person => <button className={`home-person-card ${person.watchlist === "black" ? "blacklist-outline" : ""} ${selection?.person?.slug === person.slug ? "selected" : ""}`} key={person.slug} onClick={() => selectPerson(person)} aria-pressed={selection?.person?.slug === person.slug}>
        <Photo src={person.photo} name={person.name} /><b>{person.name}</b><span><MapPin />{person.last_camera || "טרם נראה"}</span><time title={dateTime(person.last_seen)}>{relativeTime(person.last_seen)}</time>
      </button>)}</></Rail> : <Empty icon={Users} title="הוסף אדם כדי להתחיל" />}
    </section>
    {editor && <PersonEditor person={{ name: "", files: [] }} onClose={() => setEditor(false)} onSaved={() => { setEditor(false); dashboard.reload(); }} />}
    {picker && <UnknownPicker onClose={() => setPicker(false)} onSelect={value => { select(value); setPicker(false); setDetails(true); }} />}
    {playbackEvent && <Modal title="וידאו האירוע" subtitle={`${playbackEvent.camera || "מצלמה לא ידועה"} · ${dateTime(eventTime(playbackEvent))}`} onClose={() => { request.current?.abort(); setResolving(false); setPlaybackEvent(null); }} wide>
      <EventVideo eventId={playbackEvent.event_id} className={identityForEvent(playbackEvent, people)?.watchlist === "black" ? "blacklist-outline" : ""} />
      {resolving && <p role="status">מאתר פנים…</p>}
      {selection && !resolving && !candidates && <button className="button secondary" onClick={() => { setPlaybackEvent(null); setDetails(true); }}>פרטים ורצף הופעות</button>}
    </Modal>}
    {candidates && <Modal title="בחר פנים" onClose={() => { request.current?.abort(); setCandidates(null); setResolving(false); }}><div className="face-candidates">{candidates.map((sample, index) => <button key={sample.sample_id} onClick={() => { select({ name: "לא מזוהה", photo: sample.image, source: { sample_id: sample.sample_id } }); setPlaybackEvent(null); setDetails(true); }}><Photo src={sample.image} /><span>פנים {index + 1}</span></button>)}</div></Modal>}
    {details && selection && <Modal title={selection.name} onClose={() => setDetails(false)} wide>
      <div className="home-person-summary"><Photo src={selection.photo} name={selection.name} /><div><b><MapPin />{selection.person?.last_camera || rows.at(-1)?.camera || "—"}</b><time>{dateTime(selection.person?.last_seen || eventTime(rows.at(-1) || {}))}</time>{!selection.person && <span className="home-muted">התאמות דומות · זהות לא מאומתת</span>}</div></div>
      {loading ? <Loading /> : error ? <ErrorState error={error} retry={() => setRetry(v => v + 1)} /> : <><div className="home-sequence-heading"><h3>רצף הופעות</h3><span className="home-muted">{rows.length} מתוך {appearances.data?.total || 0} · מהישן לחדש</span></div>{rows.length ? <ClipSequence key={`${currentSource}-${page}`} events={rows} people={people} /> : <Empty icon={Camera} title="אין אירועים להצגה" />}{(appearances.data?.total || 0) > 60 && <HistoryPages page={page} count={rows.length} total={appearances.data.total} onChange={setPage} />}</>}
      <div className="home-detail-actions"><button className="button secondary" onClick={() => setDetails(false)}>חזרה לאירועים</button><button className="text-button" onClick={() => { location.hash = `/faces?${query(selection.source)}`; }}>חיפוש מורחב <ArrowLeft /></button></div>
    </Modal>}
  </div>;
}

function UnknownPicker({ onClose, onSelect }) {
  const unknowns = useResource("unknowns");
  return <Modal title="לא מזוהים" onClose={onClose} wide>
    {unknowns.loading ? <Loading /> : unknowns.error ? <ErrorState error={unknowns.error} retry={unknowns.reload} /> : unknowns.data?.length ? <div className="home-unknown-grid">{unknowns.data.filter(group => group.length).map((group, i) => { const face = group[0]; return <button key={face.id} onClick={() => onSelect({ name: `לא מזוהה ${i + 1}`, photo: `media/unknowns/${face.id}.jpg`, source: { unknown_id: face.id } })}><Photo src={`media/unknowns/${face.id}.jpg`} /><b>לא מזוהה {i + 1}</b><span>{group.length} תמונות דומות</span><small>{face.camera || "—"} · {dateTime(face.event_ts || face.ts)}</small></button>; })}</div> : <Empty icon={Search} title="אין פנים לא מזוהות" />}
  </Modal>;
}
