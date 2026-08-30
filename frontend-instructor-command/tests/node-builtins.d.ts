declare module "node:assert/strict" {
  type AssertMatchInput = string | RegExp;

  const assert: {
    equal(actual: unknown, expected: unknown, message?: string): void;
    deepEqual(actual: unknown, expected: unknown, message?: string): void;
    match(actual: string, regexp: AssertMatchInput, message?: string): void;
  };

  export default assert;
}

declare module "node:test" {
  type TestFn = () => void | Promise<void>;

  export default function test(name: string, fn: TestFn): void;
}
