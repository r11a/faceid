import { expect, test } from 'vitest';
import { isBlacklistedEvent, recognitionImagePath } from './alerts.js';

test('blacklist outline applies only to confirmed or reviewed identity', () => {
  const people = [{ name: 'Alice', watchlist: 'black' }];
  expect(isBlacklistedEvent({ status: 'recognized', person: 'Alice' }, people)).toBe(true);
  expect(isBlacklistedEvent({ status: 'ambiguous', person: 'Alice', probable_person: 'Alice' }, people)).toBe(false);
  expect(isBlacklistedEvent({ status: 'recognized', person: 'Alice', ground_truth: '__unknown__' }, people)).toBe(false);
  expect(isBlacklistedEvent({ status: 'unknown', ground_truth: 'Alice' }, people)).toBe(true);
});

test('recognition photos stay on the protected ingress evidence route', () => {
  expect(recognitionImagePath('api/activity/1700000000.123-abc/image')).toBe('api/activity/1700000000.123-abc/image');
  for (const path of ['https://camera.local/photo.jpg', '//camera.local/photo.jpg', 'api/activity/../image', 'api/activity/%2e%2e/image', 'api/activity/..%5c..%5csecret/image', 'api/activity/%2fsecret/image', 'api/activity/%zz/image']) {
    expect(recognitionImagePath(path)).toBeNull();
  }
});
