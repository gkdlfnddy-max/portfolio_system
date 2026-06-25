// 인증 전용 PostgreSQL 접근 — DATABASE_URL 기반 pg Pool 싱글톤(지연 생성, 재사용).
// 기존 SQLite 조회 레이어(portfolioDb.ts)와 별개. auth 테이블은 schema portfolio 에 있다.
import { Pool, type QueryResult, type QueryResultRow } from "pg";

let pool: Pool | null = null;

function getPool(): Pool {
  if (pool) return pool;
  const conn = process.env.DATABASE_URL;
  if (!conn) {
    throw new Error("DATABASE_URL 미설정 — 인증 DB 에 연결할 수 없습니다.");
  }
  pool = new Pool({
    connectionString: conn,
    max: 5,
    idleTimeoutMillis: 30_000,
  });
  return pool;
}

// 모든 인증 쿼리는 schema portfolio 를 명시한다(테이블명에 portfolio. 접두).
export async function q<T extends QueryResultRow = QueryResultRow>(
  sql: string,
  params: unknown[] = [],
): Promise<QueryResult<T>> {
  return getPool().query<T>(sql, params);
}
