/** Fixture deliberately containing banned JSX — not part of the app source. */
export function BannedFixture() {
  return <div dangerouslySetInnerHTML={{ __html: "<p>x</p>" }} />;
}
