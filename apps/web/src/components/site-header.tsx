"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ThemeToggle } from "@/components/theme-toggle";
import { cn } from "@/lib/utils";

const REPO = "https://github.com/apatha32/Covenant---Credit-early-warning-platform";
const SITE = "https://apatha32.github.io/Covenant---Credit-early-warning-platform/";

const NAV = [
  { href: "/watchlist", label: "Watchlist" },
  { href: "/model", label: "Model card" },
];

export function SiteHeader() {
  const path = usePathname();
  return (
    <header className="sticky top-0 z-20 border-b bg-background/85 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4 sm:gap-5 sm:px-6">
        <Link href="/watchlist" className="flex shrink-0 items-center gap-2 font-bold">
          <span className="grid size-[22px] place-items-center rounded-md bg-brand text-brand-ink">
            <svg viewBox="0 0 32 32" className="size-4" aria-hidden>
              <path
                d="M7 22l6-7 5 4 7-10"
                stroke="currentColor"
                strokeWidth="3.5"
                fill="none"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          Covenant
          <span className="hidden rounded-full bg-brand-soft px-2 py-0.5 text-[11px] font-semibold text-brand sm:inline">
            Live demo
          </span>
        </Link>
        <nav className="flex gap-1 overflow-x-auto">
          {NAV.map((n) => (
            <Link
              key={n.href}
              href={n.href}
              className={cn(
                "rounded-lg px-2.5 py-1.5 text-sm whitespace-nowrap text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground",
                path.startsWith(n.href) && "bg-secondary text-foreground",
              )}
            >
              {n.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-1">
          <a
            href={SITE}
            className="hidden rounded-lg px-2.5 py-1.5 text-sm text-muted-foreground hover:text-foreground md:inline"
          >
            Project site
          </a>
          <a
            href={REPO}
            className="hidden rounded-lg px-2.5 py-1.5 text-sm text-muted-foreground hover:text-foreground sm:inline"
          >
            GitHub
          </a>
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
