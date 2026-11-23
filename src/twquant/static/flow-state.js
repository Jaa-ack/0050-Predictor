export function tracedEdges(edges, selected) {
  if (!selected) return [];
  function traverse(start, origin, destination) {
    const visited = new Set([start]);
    const selectedEdges = new Set();
    const queue = [start];
    while (queue.length) {
      const node = queue.shift();
      for (const edge of edges) {
        if (edge[origin] !== node) continue;
        selectedEdges.add(edge);
        if (visited.has(edge[destination])) continue;
        visited.add(edge[destination]);
        queue.push(edge[destination]);
      }
    }
    return selectedEdges;
  }
  const downstream = traverse(selected, 'from', 'to');
  const upstream = traverse(selected, 'to', 'from');
  return edges.filter(edge => downstream.has(edge) || upstream.has(edge));
}
