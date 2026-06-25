import { z } from "zod";

// 계좌 연결 폼 검증 (클라이언트 + 서버 동일 스키마).
export const accountFormSchema = z.object({
  alias: z.string().min(1, "계좌 별칭은 필수입니다").max(50, "50자 이하로 입력하세요"),
  mode: z.enum(["paper", "live"]),
  appKey: z.string().min(1, "APP Key는 필수입니다").min(8, "APP Key가 너무 짧습니다"),
  appSecret: z.string().min(1, "APP Secret은 필수입니다").min(16, "APP Secret이 너무 짧습니다"),
  accountNo: z.string().regex(/^\d{8}$/, "계좌번호는 앞 8자리 숫자입니다"),
  productCode: z.string().regex(/^\d{2}$/, "상품코드는 2자리 숫자").default("01"),
});

export type AccountFormData = z.infer<typeof accountFormSchema>;

export type FieldErrors = Partial<Record<keyof AccountFormData, string>>;

// zod 에러 → 필드별 첫 메시지 맵.
export function toFieldErrors(err: z.ZodError): FieldErrors {
  const out: FieldErrors = {};
  for (const issue of err.issues) {
    const key = issue.path[0] as keyof AccountFormData;
    if (key && !out[key]) out[key] = issue.message;
  }
  return out;
}
