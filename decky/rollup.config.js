import deckyPlugin from '@decky/rollup';
const config = deckyPlugin();
config.output.sourcemap = false;
export default config;
