declare module "node:assert/strict" {
  type AssertMatchInput = string | RegExp;

  const assert: {
    equal(actual: unknown, expected: unknown, message?: string): void;
    deepEqual(actual: unknown, expected: unknown, message?: string): void;
    match(actual: string, regexp: AssertMatchInput, message?: string): void;
    doesNotMatch(actual: string, regexp: AssertMatchInput, message?: string): void;
    throws(block: () => unknown, error?: RegExp | ((error: unknown) => boolean)): void;
    rejects(block: () => Promise<unknown>, error?: RegExp | ((error: unknown) => boolean)): Promise<void>;
  };

  export default assert;
}

declare module "node:test" {
  type TestFn = () => void | Promise<void>;

  export default function test(name: string, fn: TestFn): void;
}
