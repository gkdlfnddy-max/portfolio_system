"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";
import { ArrowLeft, Plus, Trash2, ShieldCheck, AlertCircle } from "lucide-react";

type Row = {
  ticker: string;
  market: string;
  name: string | null;
  asset_class: string | null;
  target_weight_pct: number;
  last_price: number | null;
  verified_at: string | null;
};

const won = (n: number | null) => (n == null ? "—" : Math.round(n).toLocaleString("ko-KR") + "원");

export default function UniversePage() {
  const params = useParams();
  const id = String(params.id);

  const [rows, setRows] = useState<Row[]>([]);
  const [ticker, setTicker] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await fetch(`/api/accounts/${id}/universe`, { cache: "no-store" });
    const d = await r.json();
    setRows(d.instruments ?? []);
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  const add = async () => {
    setError(null);
    if (!/^\d{6}$/.test(ticker.trim())) {
      setError("국내 종목코드 6자리를 입력하세요 (예: 000660)");
      return;
    }
    setBusy(true);
    try {
      const r = await fetch(`/api/accounts/${id}/universe`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ticker: ticker.trim() }),
      });
      const d = await r.json();
      if (!d.ok) setError(d.error ?? "추가 실패");
      else setTicker("");
      await load();
    } catch (e: any) {
      setError(e?.message ?? "추가 실패");
    }
    setBusy(false);
  };

  const setWeight = async (t: string, weight: number) => {
    await fetch(`/api/accounts/${id}/universe`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ticker: t, weight }),
    });
    await load();
  };

  const remove = async (t: string) => {
    await fetch(`/api/accounts/${id}/universe`, {
      method: "DELETE",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ticker: t }),
    });
    await load();
  };

  const sum = Math.round(rows.reduce((a, r) => a + (r.target_weight_pct || 0), 0) * 10) / 10;

  return (
    <div className="max-w-3xl mx-auto px-5 py-10 space-y-6">
      <Link href={`/accounts/${id}`} className="text-sm text-neutral-500 flex items-center gap-1 hover:text-primary">
        <ArrowLeft className="w-4 h-4" /> 계좌 화면
      </Link>

      <div>
        <h1 className="text-2xl font-bold text-neutral-900">종목 유니버스 · 목표비중</h1>
        <p className="text-sm text-neutral-500 mt-1">
          종목코드를 입력하면 <b>한국투자증권에서 검증</b>된 종목만 추가됩니다. 목표비중을 편집하고, 빼고 싶으면 비활성화하세요.
        </p>
      </div>

      {/* 추가 */}
      <Card>
        <CardHeader>
          <CardTitle>종목 추가 (직접 입력 + KIS 검증)</CardTitle>
        </CardHeader>
        <CardBody className="space-y-3">
          <div className="flex items-end gap-2">
            <div className="flex-1">
              <Label htmlFor="ticker">국내 종목코드 (6자리)</Label>
              <Input
                id="ticker"
                placeholder="예: 000660 (SK하이닉스)"
                value={ticker}
                onChange={(e) => setTicker(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && add()}
              />
            </div>
            <Button onClick={add} disabled={busy}>
              <Plus className="w-4 h-4" /> {busy ? "검증 중…" : "추가"}
            </Button>
          </div>
          {error && (
            <p className="text-sm text-error flex items-center gap-1">
              <AlertCircle className="w-4 h-4" /> {error}
            </p>
          )}
          <p className="text-[11px] text-neutral-400">
            한글 종목명·해외주식·자동완성은 다음 단계(종목마스터 적재)에서 지원됩니다. 현재는 국내 종목코드 + 실시간 검증.
          </p>
        </CardBody>
      </Card>

      {/* 목록 */}
      <Card>
        <CardHeader className="flex items-center justify-between">
          <CardTitle>관심 종목 ({rows.length})</CardTitle>
          <span className={`text-sm font-semibold ${sum === 100 ? "text-success" : "text-warning"}`}>
            목표비중 합 {sum}%
          </span>
        </CardHeader>
        <CardBody>
          {rows.length === 0 ? (
            <div className="text-sm text-neutral-400 text-center py-8">
              아직 종목이 없습니다. 위에서 종목코드를 추가하세요.
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-neutral-400 text-xs border-b border-neutral-100">
                  <th className="text-left py-1.5">종목코드</th>
                  <th className="text-left">업종</th>
                  <th className="text-right">현재가</th>
                  <th className="text-right">목표비중(%)</th>
                  <th className="text-right"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.ticker} className="border-b border-neutral-50">
                    <td className="py-2 font-mono">
                      {r.ticker}
                      <ShieldCheck className="w-3.5 h-3.5 text-success inline ml-1" />
                    </td>
                    <td className="text-neutral-600">{r.asset_class ?? "—"}</td>
                    <td className="text-right tabular-nums">{won(r.last_price)}</td>
                    <td className="text-right">
                      <input
                        type="number"
                        defaultValue={r.target_weight_pct}
                        min={0}
                        max={100}
                        step={0.5}
                        onBlur={(e) => {
                          const v = Number(e.target.value);
                          if (Number.isFinite(v) && v !== r.target_weight_pct) setWeight(r.ticker, v);
                        }}
                        className="w-20 rounded-lg border border-neutral-200 px-2 py-0.5 text-right tabular-nums"
                      />
                    </td>
                    <td className="text-right">
                      <button
                        onClick={() => remove(r.ticker)}
                        className="text-neutral-400 hover:text-error p-1"
                        title="유니버스에서 제외"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardBody>
      </Card>

      <p className="text-xs text-neutral-400">
        이 유니버스가 의사결정 화면(drift·리밸런싱 제안)의 목표비중 기준이 됩니다 (mock 종목목록 대체).
      </p>
    </div>
  );
}
