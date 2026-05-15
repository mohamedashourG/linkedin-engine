/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // ── Trailing-slash handling for /api/* proxy ──────────────────────────
  // The FastAPI settings router is registered at prefix '/api/settings'
  // with route '/' — canonical URL has a trailing slash. Default Next.js
  // behaviour strips the trailing slash both client-side AND when
  // forwarding to the proxy destination, which makes the backend issue
  // a 307 redirect back to the trailing-slash form with an ABSOLUTE
  // backend URL. That redirect is cross-origin (3000 → 8000) and gets
  // blocked by CORS. Two flags together preserve the slash end-to-end:
  //   • trailingSlash:true — Next keeps the slash in URLs and when
  //                          forwarding to rewrite destinations
  //   • skipTrailingSlashRedirect:true — Next won't redirect
  //                          slash-less requests to slash-ful ones
  trailingSlash: true,
  skipTrailingSlashRedirect: true,
  // ── Upstream proxy timeout ────────────────────────────────────────────
  // Next's default dev-server proxy kills upstream fetches at 30s, which
  // surfaces as a generic 500 on the browser side even when the backend
  // is still happily running. For comment posting (Unipile resolve →
  // throttled POST → response can legitimately take 20-40s under the
  // pool's pacing rules), a 30s ceiling means the user sees "500
  // internal error" → clicks Send again → and now we'd be racing a
  // duplicate post against the in-flight one. The backend's atomic
  // claim catches that race, but it shouldn't even *appear* to fail
  // when the comment is actually going through. Bump to 90s so genuine
  // slow paths complete, and any real failure is a real failure.
  experimental: {
    proxyTimeout: 90_000,
  },
  async rewrites() {
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${apiUrl}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
