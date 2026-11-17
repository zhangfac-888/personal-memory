/**
 * Personal Memory DSH bundle entry.
 *
 * This is a configuration-only bundle: the actual behavior comes from
 * cordis.patch.yml, which registers DSH's MCP client against the local
 * Personal Memory service. This module exists so the npm package has a
 * valid installation entry point.
 */
export function apply(ctx) {}
