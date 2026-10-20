import { identityForEvent } from "./home.js";

export const alertReasons = { critical_camera: "מצלמה קריטית", person_notification: "התראת אדם", blacklist: "רשימה שחורה" };
export const isBlacklistedEvent = (event, people) => identityForEvent(event, people)?.watchlist === "black";
