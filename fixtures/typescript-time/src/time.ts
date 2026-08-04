/** Small fixture whose public time unit is integer milliseconds. */
export type Milliseconds = number;

export function addMilliseconds(left: Milliseconds, right: Milliseconds): Milliseconds {
  return left + right;
}
