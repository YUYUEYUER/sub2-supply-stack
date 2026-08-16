# Sub2 supply stack images

This repository publishes pinned GHCR images for:

- Sub2API `v0.1.177` with the HTTP/SSE ingress to WebSocket upstream patch.
- Sub2 Supply Bridge `1.0.19` with BugTeam API and RBAC compatibility.

The workflow checks out the official Sub2API tag at build time and applies
`patches/http-ws.patch`; the upstream source is not vendored here.

