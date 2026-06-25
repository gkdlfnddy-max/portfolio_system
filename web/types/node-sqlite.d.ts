// Node 22+/24 내장 node:sqlite 의 최소 타입 선언 (@types/node 20 미포함분 보강).
declare module "node:sqlite" {
  export class StatementSync {
    all(...params: any[]): any[];
    get(...params: any[]): any;
    run(...params: any[]): { changes: number; lastInsertRowid: number | bigint };
  }
  export class DatabaseSync {
    constructor(path: string, options?: { readOnly?: boolean; open?: boolean; enableForeignKeyConstraints?: boolean });
    prepare(sql: string): StatementSync;
    exec(sql: string): void;
    close(): void;
  }
}
