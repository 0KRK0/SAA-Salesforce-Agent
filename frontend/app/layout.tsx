import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Salesforce AI Agent",
  description: "Natural-language Salesforce engineering and operations agent",
};

/**
 * The interface follows the operating system's appearance, both ways.
 *
 * `themeColor` per scheme keeps the browser chrome from cutting a hard edge
 * against the page — on a translucent interface that seam is the first thing
 * that reads as "web app pretending to be native".
 */
export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#eef1f6" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0d13" },
  ],
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
