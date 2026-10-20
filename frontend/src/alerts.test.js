import { expect, test } from 'vitest';
import { isBlacklistedEvent } from './alerts.js';

test('blacklist outline applies only to confirmed or reviewed identity', () => {
  const people = [{ name: 'Alice', watchlist: 'black' }];
  expect(isBlacklistedEvent({ status: 'recognized', person: 'Alice' }, people)).toBe(true);
  expect(isBlacklistedEvent({ status: 'ambiguous', person: 'Alice', probable_person: 'Alice' }, people)).toBe(false);
  expect(isBlacklistedEvent({ status: 'recognized', person: 'Alice', ground_truth: '__unknown__' }, people)).toBe(false);
  expect(isBlacklistedEvent({ status: 'unknown', ground_truth: 'Alice' }, people)).toBe(true);
});
