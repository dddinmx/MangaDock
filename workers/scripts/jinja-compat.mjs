// Keep Python/Jinja truthiness, including empty lists/dicts, in the precompiled templates.
export function pybool(value) {
  if (value == null || value === false || value === 0 || value === "")
    return false;
  if (Array.isArray(value)) return value.length > 0;
  if (value instanceof Map || value instanceof Set) return value.size > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}
export function install(nunjucks, engine) {
  engine.addFilter("pybool", pybool);
  engine.addGlobal("__pybool", pybool);
  const proto = nunjucks.compiler.Compiler.prototype,
    nodes = nunjucks.nodes;
  const call = (node) =>
    new nodes.FunCall(
      node.lineno,
      node.colno,
      new nodes.Symbol(node.lineno, node.colno, "__pybool"),
      new nodes.NodeList(node.lineno, node.colno, [node]),
    );
  for (const method of ["compileIf", "compileInlineIf"]) {
    const original = proto[method];
    proto[method] = function (node, frame, ...args) {
      const condition = node.cond;
      node.cond = call(condition);
      try {
        return original.call(this, node, frame, ...args);
      } finally {
        node.cond = condition;
      }
    };
  }
  for (const [method, and] of [
    ["compileAnd", true],
    ["compileOr", false],
  ])
    proto[method] = function (node, frame) {
      const temp = this._tmpid();
      this._emit(
        "(function(" + temp + "){return env.getFilter('pybool')(" + temp + ")?",
      );
      if (and) this.compile(node.right, frame);
      else this._emit(temp);
      this._emit(":");
      if (and) this._emit(temp);
      else this.compile(node.right, frame);
      this._emit("})(");
      this.compile(node.left, frame);
      this._emit(")");
    };
  proto.compileNot = function (node, frame) {
    this._emit("(!env.getFilter('pybool')(");
    this.compile(node.target, frame);
    this._emit("))");
  };
}
