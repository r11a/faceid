import React, { useEffect, useRef, useState } from "react";
import { api, assetUrl } from "./api.js";

export function EventVideo({ eventId, nextEventId, onEnded, onUnavailable, className = "" }) {
  return <Clip key={eventId} eventId={eventId} nextEventId={nextEventId} onEnded={onEnded} onUnavailable={onUnavailable} className={className} />;
}

function Clip({ eventId, nextEventId, onEnded, onUnavailable, className }) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState("loading");
  const [loaded, setLoaded] = useState(false);
  const [diagnostic, setDiagnostic] = useState(null);
  const [seekable, setSeekable] = useState(false);
  const prepared = useRef(false);
  const unavailableAttempt = useRef(null);
  const video = useRef(null);
  const mediaError = useRef(0);
  const unavailable = useRef(onUnavailable);
  unavailable.current = onUnavailable;
  const retry = () => {
    setState("loading"); setLoaded(false); setDiagnostic(null); setAttempt(value => value + 1);
  };
  const play = () => {
    const element = video.current;
    element?.play()?.catch(error => {
      if (video.current === element && error.name === "NotAllowedError") setState(value => value === "error" ? value : "blocked");
    });
  };
  useEffect(() => {
    const element = video.current;
    if (element && !element.getAttribute("src")) element.setAttribute("src", src);
    play();
    return () => {
      // Detached media can keep downloading and decoding on mobile WebViews.
      element?.pause();
      element?.removeAttribute("src");
      element?.load();
    };
  }, [attempt]);
  useEffect(() => {
    if (state !== "error") return undefined;
    let active = true;
    let timer;
    const controller = new AbortController();
    (async () => {
      try {
        const result = await api(`activity/${encodeURIComponent(eventId)}/clip/status?probe=true`, { signal: controller.signal });
        if (!active) return;
        if (result?.ready && result?.seekable === false && !prepared.current) {
          prepared.current = true;
          setSeekable(true);
          retry();
          return;
        }
        const codecFailure = result?.ready && [3, 4].includes(mediaError.current);
        setDiagnostic(codecFailure
          ? "הקליפ קיים, אך המכשיר לא הצליח לפענח את פורמט הווידאו או האודיו. אפשר לנסות שוב; אם התקלה חוזרת, יש לבדוק את פורמט ההקלטה במצלמה (H.264 / AAC)."
          : result?.message || "לא התקבל פירוט שגיאה מ־Frigate.");
        // A fresh recording may not have ready segments yet. Never retry permission/TLS errors.
        if ((result?.retryable || (result?.ready && mediaError.current === 2)) && attempt < 3) {
          timer = globalThis.setTimeout(retry, result?.retryable ? 5000 : 1500);
        } else if (!result?.ready && !result?.retryable && [400, 404].includes(result?.upstream_http) && unavailableAttempt.current !== attempt) {
          // Only a confirmed missing recording may advance a playlist. A network,
          // codec or permission failure must not silently skip every event.
          unavailableAttempt.current = attempt;
          unavailable.current?.();
        }
      } catch (error) { if (active) setDiagnostic(error.message); }
    })();
    return () => { active = false; controller.abort(); globalThis.clearTimeout(timer); };
  }, [state, attempt, eventId]);
  const src = assetUrl(`api/activity/${encodeURIComponent(eventId)}/clip${seekable ? "?seekable=true" : ""}`);
  return <div className={`event-video ${className}`}>
    <video ref={video} key={attempt} controls autoPlay muted playsInline preload="auto" src={src}
      onLoadStart={() => setState(value => ["error", "blocked"].includes(value) ? value : "loading")}
      onWaiting={() => setState(value => ["error", "blocked"].includes(value) ? value : "buffering")}
      onLoadedData={() => setState(value => ["error", "blocked", "playing"].includes(value) ? value : "ready")}
      onPlaying={() => { setLoaded(true); setState("playing"); }}
      onCanPlay={() => setState(value => ["error", "blocked", "playing"].includes(value) ? value : "ready")}
      onError={event => {
        mediaError.current = event?.currentTarget?.error?.code || 0;
        setLoaded(false);
        setState("error");
      }} onEnded={onEnded} />
    {(state === "loading" || state === "buffering") && <p role="status">{state === "loading" ? "טוען וידאו…" : "ממתין לנתוני וידאו…"}</p>}
    {state === "blocked" && <p role="status"><button className="button secondary" onClick={play}>נגן וידאו</button></p>}
    {state === "error" && <div role="alert"><p>{diagnostic || "בודק מדוע הקליפ לא נטען…"}</p><button className="button secondary" onClick={() => { setSeekable(false); retry(); }}>נסה שוב</button></div>}
    {loaded && nextEventId && nextEventId !== eventId && <video key={nextEventId} hidden aria-hidden="true" tabIndex={-1} preload="metadata" src={assetUrl(`api/activity/${encodeURIComponent(nextEventId)}/clip`)} />}
  </div>;
}
