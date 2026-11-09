declare module "node:assert/strict" {
  type AssertMatchInput = string | RegExp;
  type AssertExpectation = RegExp | ((error: unknown) => boolean);

  const assert: {
    equal(actual: unknown, expected: unknown, message?: string): void;
    deepEqual(actual: unknown, expected: unknown, message?: string): void;
    match(actual: string, regexp: AssertMatchInput, message?: string): void;
    doesNotMatch(actual: string, regexp: AssertMatchInput, message?: string): void;
    throws(fn: () => unknown, expected?: AssertExpectation, message?: string): void;
    rejects(fn: () => Promise<unknown>, expected?: AssertExpectation, message?: string): Promise<void>;
  };

  export default assert;
}

declare module "node:test" {
  type TestFn = () => void | Promise<void>;

  export default function test(name: string, fn: TestFn): void;
}
