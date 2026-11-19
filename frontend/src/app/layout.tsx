import type { Metadata } from "next";
import { Source_Serif_4 } from "next/font/google";
import "./globals.css";
import { AppShell } from "@/components/sites/claude-shell/AppShell";

const serif = Source_Serif_4({
  variable: "--font-serif",
  subsets: ["latin"],
  weight: ["300", "400", "500"],
});

export const metadata: Metadata = {
  title: "Claude Hardware",
  description: "A hardware design workspace in Claude",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${serif.variable} h-full antialiased dark`}>
      <body className="h-full cs-app">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
