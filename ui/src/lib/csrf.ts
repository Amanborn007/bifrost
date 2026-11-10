/**
 * Headers every mutating browser request must carry. The API's CSRF
 * middleware rejects POST/PUT/PATCH/DELETE from a browser without them.
 */
export const CSRF_HEADER_NAME = "X-Requested-With";
export const CSRF_HEADER_VALUE = "bifrost-ui";

export function csrfHeaders(): Record<string, string> {
  return { [CSRF_HEADER_NAME]: CSRF_HEADER_VALUE };
}
