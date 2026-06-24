import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import './globals.css';

export const metadata: Metadata = {
  title: 'JARVIS Sovereign Command Node',
  description:
    'Read-only mission-control dashboard for the JARVIS Ouroboros loop (Phase 1).',
};

export default function RootLayout({
  children,
}: {
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
