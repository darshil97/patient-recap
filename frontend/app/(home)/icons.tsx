/**
 * Line icons for the public home page, inline so the page needs no icon package
 * and no client JavaScript. All decorative: every one sits next to text that
 * already says what it means, so each is aria-hidden.
 */
import type { ReactNode } from "react";

function Icon({ children, size = 20 }: { children: ReactNode; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

type IconProps = { size?: number };

export const CheckIcon = ({ size = 14 }: IconProps) => (
  <Icon size={size}>
    <path d="M5 12.5l4.2 4.2L19 7" />
  </Icon>
);

export const ArrowRightIcon = ({ size = 16 }: IconProps) => (
  <Icon size={size}>
    <path d="M5 12h14M13 6l6 6-6 6" />
  </Icon>
);

export const LeafIcon = ({ size = 12 }: IconProps) => (
  <Icon size={size}>
    <path d="M5 19c0-8 5-14 15-14 0 10-6 15-14 15" />
    <path d="M5 19l7-7" />
  </Icon>
);

export const CalendarIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <rect x="3.5" y="5" width="17" height="15.5" rx="2.5" />
    <path d="M3.5 10h17M8 3v4M16 3v4" />
  </Icon>
);

export const MicIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
  </Icon>
);

export const BriefIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <path d="M7 3h7l5 5v13H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" />
    <path d="M14 3v5h5M9 13h7M9 17h5" />
  </Icon>
);

export const PhoneIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <path d="M6 3.5h3l1.8 4.6-2.3 1.4a11 11 0 0 0 6 6l1.4-2.3 4.6 1.8v3a2 2 0 0 1-2.2 2A16.5 16.5 0 0 1 4 5.7a2 2 0 0 1 2-2.2z" />
  </Icon>
);

export const HistoryIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <path d="M3.5 12a8.5 8.5 0 1 0 2.5-6" />
    <path d="M3.5 4v4h4M12 7.5V12l3 2" />
  </Icon>
);

export const UserCheckIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <circle cx="9" cy="8" r="3.8" />
    <path d="M2.5 20.5a6.5 6.5 0 0 1 13 0M16 11.5l2 2 4-4" />
  </Icon>
);

export const BrowserIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <rect x="3" y="4" width="18" height="16" rx="2.5" />
    <path d="M3 9h18M7 6.5h.01M10 6.5h.01" />
  </Icon>
);

export const BoltIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <path d="M13 2.5L4.5 13.5H11l-1 8 8.5-11H12z" />
  </Icon>
);

export const MessageIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <path d="M4 5h16v11H9l-5 4z" />
    <path d="M8 9.5h8M8 12.5h5" />
  </Icon>
);

export const TagIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <path d="M3.5 12.2V4h8.2l9 9-8.2 8.2z" />
    <circle cx="8" cy="8.5" r="1.4" />
  </Icon>
);

export const LockIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <rect x="4.5" y="10.5" width="15" height="10" rx="2.5" />
    <path d="M8 10.5V7.5a4 4 0 0 1 8 0v3M12 14.5v2.5" />
  </Icon>
);

export const ServerIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <rect x="3.5" y="4" width="17" height="7" rx="2" />
    <rect x="3.5" y="13" width="17" height="7" rx="2" />
    <path d="M7.5 7.5h.01M7.5 16.5h.01M11 7.5h6M11 16.5h6" />
  </Icon>
);

export const EyeOffIcon = ({ size }: IconProps) => (
  <Icon size={size}>
    <path d="M3 3l18 18" />
    <path d="M10.6 5.1A10.8 10.8 0 0 1 12 5c6 0 9.5 7 9.5 7a17 17 0 0 1-3.1 4M6.6 6.6C3.9 8.4 2.5 12 2.5 12s3.5 7 9.5 7a9.6 9.6 0 0 0 5.4-1.6" />
    <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
  </Icon>
);

export const MinusIcon = ({ size = 14 }: IconProps) => (
  <Icon size={size}>
    <path d="M6 12h12" />
  </Icon>
);

