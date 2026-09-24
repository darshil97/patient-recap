import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getSession = vi.fn();
vi.mock("@/lib/auth/server", () => ({
  auth: { getSession: () => getSession() },
}));

const { GET, POST, PATCH } = await import("@/app/api/[...path]/route");

const SESSION = {
  data: {
    user: { id: "neon-user-aaa", email: "doctor.a@example.com", name: "Doctor A" },
  },
};

function ctx(path: string[]) {
  return { params: Promise.resolve({ path }) };
}

/**
 * This route is the entire boundary between the browser and FastAPI: it is where
 * the session is checked, where the API key is attached, and where the backend
 * learns which doctor it is serving. It had no tests.
 *
 * The header allowlist is the load-bearing part. Since the backend derives
 * tenancy from X-Doctor-*, a proxy that copied request headers through would let
 * a browser name any doctor it liked.
 */
describe("BFF proxy", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    getSession.mockReset().mockResolvedValue(SESSION);
    fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json", "x-total-count": "42" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("BACKEND_URL", "http://127.0.0.1:8000");
    vi.stubEnv("BACKEND_API_KEY", "shared-secret");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  const forwarded = () => new Headers(fetchMock.mock.calls[0][1].headers);

  it("attaches the API key and the signed-in doctor", async () => {
    await GET(new Request("https://localhost:3000/api/v1/patients"), ctx(["v1", "patients"]));

    const headers = forwarded();
    expect(headers.get("X-API-Key")).toBe("shared-secret");
    expect(headers.get("X-Doctor-Auth-Id")).toBe("neon-user-aaa");
    expect(headers.get("X-Doctor-Email")).toBe("doctor.a@example.com");
  });

  it("ignores doctor headers sent by the browser", async () => {
    // The one that matters. These are not in the allowlist, and the real values
    // are set after the copy loop, so a client cannot act as another doctor.
    await GET(
      new Request("https://localhost:3000/api/v1/patients", {
        headers: {
          "X-Doctor-Auth-Id": "neon-user-attacker",
          "X-Doctor-Email": "someone.else@example.com",
          "X-API-Key": "guessed",
        },
      }),
      ctx(["v1", "patients"]),
    );

    const headers = forwarded();
    expect(headers.get("X-Doctor-Auth-Id")).toBe("neon-user-aaa");
    expect(headers.get("X-Doctor-Email")).toBe("doctor.a@example.com");
    expect(headers.get("X-API-Key")).toBe("shared-secret");
  });

  it("does not forward the session cookie to the backend", async () => {
    await GET(
      new Request("https://localhost:3000/api/v1/patients", {
        headers: { cookie: "__Secure-session=secret" },
      }),
      ctx(["v1", "patients"]),
    );
    expect(forwarded().get("cookie")).toBeNull();
  });

  it("copies X-Total-Count back, or paginated lists lose their totals", async () => {
    const response = await GET(
      new Request("https://localhost:3000/api/v1/patients"),
      ctx(["v1", "patients"]),
    );
    expect(response.headers.get("x-total-count")).toBe("42");
  });

  it("refuses without a session, before calling the backend", async () => {
    getSession.mockResolvedValue({ data: null });
    const response = await GET(
      new Request("https://localhost:3000/api/v1/patients"),
      ctx(["v1", "patients"]),
    );
    expect(response.status).toBe(401);
    expect(await response.json()).toMatchObject({ code: "unauthorized" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses a session with no email, which the backend could not attribute", async () => {
    getSession.mockResolvedValue({ data: { user: { id: "neon-user-aaa" } } });
    const response = await GET(
      new Request("https://localhost:3000/api/v1/patients"),
      ctx(["v1", "patients"]),
    );
    expect(response.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never tunnels auth routes through to FastAPI", async () => {
    const response = await GET(
      new Request("https://localhost:3000/api/auth/get-session"),
      ctx(["auth", "get-session"]),
    );
    expect(response.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("preserves a multipart content-type verbatim, boundary and all", async () => {
    // Re-encoding would mint a new boundary that no longer matches the body, and
    // every transcript upload would fail.
    const boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW";
    await POST(
      new Request("https://localhost:3000/api/v1/visits/x/transcript", {
        method: "POST",
        headers: { "content-type": `multipart/form-data; boundary=${boundary}` },
        body: "--" + boundary + "\r\n\r\n",
      }),
      ctx(["v1", "visits", "x", "transcript"]),
    );
    expect(forwarded().get("content-type")).toBe(
      `multipart/form-data; boundary=${boundary}`,
    );
  });

  it("returns a 204 with no body, because constructing one throws", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    const response = await GET(
      new Request("https://localhost:3000/api/v1/visits/x/job"),
      ctx(["v1", "visits", "x", "job"]),
    );
    expect(response.status).toBe(204);
    expect(response.body).toBeNull();
  });

  it("reports a timeout as 504, distinct from unreachable", async () => {
    fetchMock.mockRejectedValue(
      new DOMException("The operation timed out.", "TimeoutError"),
    );
    const response = await GET(
      new Request("https://localhost:3000/api/v1/patients"),
      ctx(["v1", "patients"]),
    );
    expect(response.status).toBe(504);
    expect(await response.json()).toMatchObject({ code: "backend_timeout" });
  });

  it("reports a refused connection as 502", async () => {
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));
    const response = await GET(
      new Request("https://localhost:3000/api/v1/patients"),
      ctx(["v1", "patients"]),
    );
    expect(response.status).toBe(502);
    expect(await response.json()).toMatchObject({ code: "backend_unreachable" });
  });

  it("passes the query string through untouched", async () => {
    await GET(
      new Request("https://localhost:3000/api/v1/patients?q=riya&limit=50"),
      ctx(["v1", "patients"]),
    );
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/v1/patients?q=riya&limit=50",
    );
  });

  it("forwards the method and body of a PATCH", async () => {
    await PATCH(
      new Request("https://localhost:3000/api/v1/visits/x", {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ visit_type: "follow_up" }),
      }),
      ctx(["v1", "visits", "x"]),
    );
    const init = fetchMock.mock.calls[0][1];
    expect(init.method).toBe("PATCH");
    expect(new TextDecoder().decode(init.body)).toBe(
      JSON.stringify({ visit_type: "follow_up" }),
    );
  });
});
