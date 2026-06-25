import Link from "next/link";
import { Sparkles } from "lucide-react";
import { AuthMenu } from "./auth/AuthMenu";

export function Nav() {
  const links = [
    { href: "/", label: "홈" },
    { href: "/accounts/new", label: "계좌 연결" },
  ];
  return (
    <header className="sticky top-0 z-30 bg-white/80 backdrop-blur border-b border-neutral-100">
      <div className="max-w-6xl mx-auto px-5 h-14 flex items-center justify-between">
        <Link href="/" className="flex items-center gap-2 font-bold text-neutral-900">
          <Sparkles className="w-5 h-5 text-primary" />
          Portfolio OS
        </Link>
        <nav className="flex items-center gap-1">
          {links.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className="px-3 py-1.5 text-sm text-neutral-700 hover:text-primary hover:bg-neutral-50 rounded-lg"
            >
              {l.label}
            </Link>
          ))}
          <AuthMenu />
        </nav>
      </div>
    </header>
  );
}
