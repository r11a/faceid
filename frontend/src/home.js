export const eventTime = row => Number(row.start_ts ?? row.updated_ts ?? 0);
export const chronological = rows => [...rows].sort((a, b) => eventTime(a) - eventTime(b) || a.event_id.localeCompare(b.event_id));
export const latestPeople = people => [...people].sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0) || a.name?.localeCompare(b.name || "") || 0);
export const clipAfter = (index, length) => index + 1 < length ? index + 1 : null;
export function identityForEvent(event, people) {
  const name = event.ground_truth || (event.status === "recognized" ? event.person : null);
  return name && name !== "__unknown__" ? people.find(person => person.name === name) || null : null;
}
