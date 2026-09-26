#!/usr/bin/env node

import { main } from "./RxnMiner/cli/src/main.mjs";

process.exitCode = await main(process.argv.slice(2));
