import React, { useRef, useState } from "react";
import { assetUrl } from "./api.js";

export function EventVideo({ eventId, nextEventId, onEnded, onUnavailable, className = "" }) {
  return <Clip key={eventId} eventId={eventId} nextEventId={nextEventId} onEnded={onEnded} onUnavailable={onUnavailable} className={className} />;
}

function Clip({ eventId, nextEventId, onEnded, onUnavailable, className }) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState("loading");
  const [loaded, setLoaded] = useState(false);
  const unavailableAttempt = useRef(null);
  const src = assetUrl(`api/activity/${encodeURIComponent(eventId)}/clip`);
  return <div className={`event-video ${className}`}>
    <video key={attempt} controls autoPlay playsInline preload="auto" src={src}
      onLoadStart={() => setState("loading")} onWaiting={() => setState("buffering")}
      onLoadedData={() => { setLoaded(true); setState("ready"); }}
      onPlaying={() => setState("playing")} onCanPlay={() => setState("ready")}
      onError={() => {
        setState("error");
        if (unavailableAttempt.current !== attempt) {
          unavailableAttempt.current = attempt;
          onUnavailable?.();
        }
      }} onEnded={onEnded} />
    {(state === "loading" || state === "buffering") && <p role="status">{state === "loading" ? "טוען וידאו…" : "ממתין לנתוני וידאו…"}</p>}
    {state === "error" && <div role="alert"><p>הקליפ אינו זמין כרגע. ייתכן שהאירוע עדיין מוקלט או שההקלטה נמחקה מ־Frigate.</p><button className="button secondary" onClick={() => { setState("loading"); setLoaded(false); setAttempt(value => value + 1); }}>נסה שוב</button></div>}
    {loaded && nextEventId && nextEventId !== eventId && <video key={nextEventId} hidden aria-hidden="true" tabIndex={-1} preload="metadata" src={assetUrl(`api/activity/${encodeURIComponent(nextEventId)}/clip`)} />}
  </div>;
}
