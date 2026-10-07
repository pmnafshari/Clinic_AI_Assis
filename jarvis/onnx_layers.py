"""The weights of openWakeWord's embedding_model.onnx, read straight from the sha256-pinned file for jarvis/embedding.py.

Only the protobuf fields needed are read (ONNX field numbers), so no onnx package is installed. Every assumption the
streaming code makes is checked on the graph itself: a different network raises ValueError instead of being run wrongly.
"""
import numpy as np


def _varint(b, i):
    value = shift = 0
    while True:
        byte = b[i]
        i += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, i


def _fields(b):
    """-> (field number, wire type, value) for each field of one protobuf message."""
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(b, i)
        elif wire == 1:
            value, i = b[i:i + 8], i + 8
        elif wire == 2:
            n, i = _varint(b, i)
            value, i = b[i:i + n], i + n
        elif wire == 5:
            value, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"unexpected protobuf wire type {wire}")
        yield number, wire, value


def _ints(wire, value):
    if wire == 0:
        return [value if value < 1 << 63 else value - (1 << 64)]
    out, i = [], 0
    while i < len(value):
        v, i = _varint(value, i)
        out.append(v if v < 1 << 63 else v - (1 << 64))
    return out


def _tensor(b):
    """TensorProto: dims 1, data_type 2, float_data 4, int64_data 7, name 8, raw_data 9."""
    dims, kind, name, raw, floats, longs = [], None, None, None, [], []
    for number, wire, value in _fields(b):
        if number == 1:
            dims += _ints(wire, value)
        elif number == 2:
            kind = value
        elif number == 4:
            floats.append(np.frombuffer(value, "<f4"))
        elif number == 7:
            longs += _ints(wire, value)
        elif number == 8:
            name = value.decode()
        elif number == 9:
            raw = value
    if raw is not None:
        array = np.frombuffer(raw, {1: "<f4", 7: "<i8"}[kind]).copy()
    elif floats:
        array = np.concatenate(floats)
    else:
        array = np.array(longs, np.int64)
    return name, array.reshape(dims) if dims else array


def _node(b):
    """NodeProto: input 1, output 2, name 3, op_type 4, attribute 5 (name 1, f 2, i 3, ints 8)."""
    node = {"in": [], "op": None, "attrs": {}}
    for number, wire, value in _fields(b):
        if number == 1:
            node["in"].append(value.decode())
        elif number == 4:
            node["op"] = value.decode()
        elif number == 5:
            attr = {}
            for n, w, v in _fields(value):
                if n == 1:
                    attr["name"] = v.decode()
                elif n == 2:
                    attr["f"] = float(np.frombuffer(v, "<f4")[0])
                elif n == 3:
                    attr["i"] = v
                elif n == 8:
                    attr.setdefault("ints", []).extend(_ints(w, v))
            node["attrs"][attr.pop("name")] = attr
    return node


def read(path):
    """-> (nodes in order, initialisers by name) of an ONNX model: ModelProto.graph 7, GraphProto node 1, initializer 5."""
    b = open(path, "rb").read()
    graph = next((value for number, _w, value in _fields(b) if number == 7), None)
    if graph is None:
        raise ValueError("no graph in the model file")
    nodes, inits = [], {}
    for number, _w, value in _fields(graph):
        if number == 1:
            nodes.append(_node(value))
        elif number == 5:
            name, array = _tensor(value)
            inits[name] = array
    return nodes, inits


def _check(ok, what):
    if not ok:
        raise ValueError(f"embedding model is not the expected network: {what}")


def layers(path):
    """-> ("conv", w [out, in, time, freq], bias, frequency padding, activation) or ("pool", time), in order."""
    try:
        return _layers(*read(path))
    except (IndexError, KeyError, TypeError, UnicodeDecodeError) as e:     # a damaged file, not a crash
        raise ValueError(f"embedding model could not be read ({type(e).__name__})") from None


def _layers(nodes, inits):
    _check(nodes and nodes[0]["op"] == "Reshape" and inits[nodes[0]["in"][1]].tolist() == [-1, 1, 76, 32],
           "input is not [time 76, freq 32]")
    out = []
    for node in nodes[1:-1]:
        op, a = node["op"], node["attrs"]
        if op == "Conv":
            w = inits[node["in"][1]]
            b = inits[node["in"][2]] if len(node["in"]) > 2 else np.zeros(w.shape[0], np.float32)
            pads = a.get("pads", {}).get("ints", [0, 0, 0, 0])
            _check(a["strides"]["ints"] == [1, 1] and a["dilations"]["ints"] == [1, 1] and a["group"]["i"] == 1,
                   "a convolution with strides, dilation or groups")
            _check(pads[0] == pads[2] == 0, "time is padded")
            _check(pads[1] == pads[3] == (w.shape[3] - 1) // 2, "frequency padding changes the size")
            out.append(["conv", w.astype(np.float32), b.astype(np.float32), pads[1], False])
        elif op == "LeakyRelu":
            _check(out and out[-1][0] == "conv" and abs(a["alpha"]["f"] - 0.2) < 1e-7, "LeakyReLU is not 0.2")
            out[-1][4] = True
        elif op == "Max":
            _check(out and out[-1][4] and abs(float(inits[node["in"][1]].ravel()[0]) + 0.4) < 1e-7,
                   "the floor after LeakyReLU is not -0.4")
        elif op == "MaxPool":
            k = a["kernel_shape"]["ints"]
            _check(k == a["strides"]["ints"] and k[1] == 2 and k[0] in (1, 2), "a pool that is not 1x2 or 2x2")
            out.append(["pool", k[0]])
        else:
            _check(False, f"unexpected {op}")
    _check(nodes[-1]["op"] == "Reshape" and out and out[-1][0] == "conv" and not out[-1][4], "the last layer")
    return [tuple(x) for x in out]
