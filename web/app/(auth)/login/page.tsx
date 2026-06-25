import { Suspense } from "react";
import { LoginForm } from "@/components/auth/LoginForm";

export const dynamic = "force-dynamic";

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="py-16 text-center text-sm text-neutral-400">불러오는 중…</div>}>
      <LoginForm />
    </Suspense>
  );
}
