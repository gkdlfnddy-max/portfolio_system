import { Badge } from "@/components/ui/Badge";
import { Building2 } from "lucide-react";

// 증권사 배지 — broker 별(한국투자증권 / 키움증권). 없으면 한국투자증권(kis) 기본.
// 표시 전용(서버가 broker 를 truth 로 제공). 색만 구분.
const MAP: Record<string, { label: string; cls: string }> = {
  kis: { label: "한국투자증권", cls: "bg-blue-50 text-blue-700" },
  kiwoom: { label: "키움증권", cls: "bg-amber-50 text-amber-700" },
};

export function BrokerBadge({ broker, className = "" }: { broker: string | null | undefined; className?: string }) {
  const key = String(broker ?? "kis").trim().toLowerCase();
  const m = MAP[key] ?? MAP.kis;
  return (
    <Badge className={`${m.cls} inline-flex items-center gap-1 ${className}`}>
      <Building2 className="w-3 h-3" /> {m.label}
    </Badge>
  );
}
