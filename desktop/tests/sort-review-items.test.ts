import { expect, test } from "vitest";
import { sortReviewItems, type ReviewVerdictItem } from "../src/pages/JdManagementPage";

type Item = { result_id: string; score: number; jd_id: string };

function verdicts(entries: Record<string, string>): Record<string, ReviewVerdictItem> {
  const map: Record<string, ReviewVerdictItem> = {};
  for (const [id, v] of Object.entries(entries)) map[id] = { verdict: v };
  return map;
}

test("review completed: recommend before pending before reject, then score desc", () => {
  const items: Item[] = [
    { result_id: "r1", score: 0.5, jd_id: "a" },
    { result_id: "r2", score: 0.9, jd_id: "b" },
    { result_id: "r3", score: 0.7, jd_id: "c" },
    { result_id: "r4", score: 0.8, jd_id: "d" },
  ];
  const v = verdicts({ r1: "reject", r2: "recommend", r3: "pending", r4: "recommend" });
  const sorted = sortReviewItems(items, v, true);
  expect(sorted.map((x) => x.result_id)).toEqual(["r2", "r4", "r3", "r1"]);
});

test("review failed: keep base score order", () => {
  const items: Item[] = [
    { result_id: "a", score: 0.4, jd_id: "a" },
    { result_id: "b", score: 0.9, jd_id: "b" },
  ];
  const sorted = sortReviewItems(items, {}, false);
  expect(sorted.map((x) => x.result_id)).toEqual(["b", "a"]);
});

test("pagination slices the sorted array (not per-page)", () => {
  const items: Item[] = Array.from({ length: 15 }, (_, i) => ({
    result_id: `r${i}`,
    score: i / 100,
    jd_id: `jd${i}`,
  }));
  const v = verdicts({ r14: "recommend", r0: "recommend" });
  const sorted = sortReviewItems(items, v, true);
  const page1 = sorted.slice(0, 10);
  const page2 = sorted.slice(10, 20);
  expect(page1.length).toBe(10);
  expect(page2.length).toBe(5);
  expect(page1[0].result_id).toBe("r14");
  expect(page2[0].result_id).toBe("r5");
});
