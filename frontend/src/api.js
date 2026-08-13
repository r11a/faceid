const basePath = (() => {
  const path = window.location.pathname;
  const marker = path.match(/^(.*?\/)(?:ui-[^/]+)?$/);
  return marker ? marker[1] : path.endsWith("/") ? path : `${path}/`;
})();

export const assetUrl = (path) => `${basePath}${String(path).replace(/^\//, "")}`;

export async function api(path, options = {}) {
  const isForm = options.body instanceof FormData;
  const response = await fetch(assetUrl(`api/${String(path).replace(/^api\//, "")}`), {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
    headers: isForm ? options.headers : {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const error = new Error(body?.detail || body?.error || body || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
}

export function query(values) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== "" && value !== null && value !== undefined) params.set(key, value);
  });
  return params.toString();
}
