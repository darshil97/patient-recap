"use client";

import Link from "next/link";
import { APP_HOME } from "@/lib/routes";

export function NotFoundPage() {
  return (
    <div className="page">
      <div className="empty">
        <p className="empty-title">Page not found</p>
        <p className="empty-text">
          The page you were looking for doesn&rsquo;t exist.
        </p>
        <Link href={APP_HOME} className="btn btn--primary">
          Back to Today
        </Link>
      </div>
    </div>
  );
}
