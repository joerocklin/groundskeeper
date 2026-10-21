// Loads the panel source in a bare vm context so pure helpers can be tested
// from Python (tests/test_panel_zones.py). Prints JSON of `expr`'s value.
const vm = require('node:vm');
const fs = require('node:fs');
const [, , file, expr] = process.argv;
const ctx = vm.createContext({
  HTMLElement: class {},
  customElements: { define() {} },
});
vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);
process.stdout.write(JSON.stringify(vm.runInContext(expr, ctx)));
