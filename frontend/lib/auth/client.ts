"use client";

/**
 * The browser-side Neon Auth client.
 *
 * Safe to import from client components: it talks to our own
 * /api/auth/[...path] route, which is where the server-side instance holds the
 * secrets. No credentials are present in this module.
 */
import { createAuthClient } from "@neondatabase/auth/next";

export const authClient = createAuthClient();
