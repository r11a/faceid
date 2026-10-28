import { identityForEvent } from "./home.js";

export const alertReasons = { critical_camera: "מצלמה קריטית", person_notification: "התראת אדם", blacklist: "רשימה שחורה" };
export const isBlacklistedEvent = (event, people) => identityForEvent(event, people)?.watchlist === "black";

export function recognitionImagePath(path) {
  if (typeof path !== "string") return null;
  const match = path.match(/^api\/activity\/([a-zA-Z0-9_.~%!-]+)\/image$/);
  if (!match) return null;
  try {
    const eventId = decodeURIComponent(match[1]);
    if (!eventId || eventId === "." || eventId === ".." || /[/\\\x00-\x1f]/.test(eventId)) return null;
  } catch { return null; }
  return path;
}
