/** The rule of one Vault item, as the server builds it (#754). */

/** Characters with a meaning in a glob: such a name cannot be its own rule. */
const GLOB_CHARACTERS = /[*?[\]\\]/;
export const hasGlobCharacters = (path: string) => GLOB_CHARACTERS.test(path);
/** `path/**` for a folder (an Excluded Folder), the exact path for a file. */
export const ownRule = (path: string, isDirectory: boolean) => isDirectory ? `${path}/**` : path;
/** The «Esclusioni» section of the Vault settings. */
export const EXCLUSIONS_SECTION = "exclusions";
