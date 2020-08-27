import itertools

import numpy as np
import dace

import gt4py.ir as gt_ir
from gt4py.utils import text as gt_text
from gt4py.utils.attrib import attribute, attribkwclass as attribclass


@attribclass
class MappedMemletInfo:
    outer_name = attribute(of=str)
    local_name = attribute(of=str)
    subset_str = attribute(of=str, optional=True)
    offset = attribute(of=dict)
    dynamic = attribute(of=bool)


def local_name(name, offset, is_target):
    CONFIG = dict(local_name_prefix="_gt_loc__", local_name_prefix_out="_gt_loc_out__")
    prefix = CONFIG["local_name_prefix_out"] if is_target else CONFIG["local_name_prefix"]

    offset_strs = []
    offset = offset or dict()

    for k, v in offset.items():
        offset_strs.append(k + ("p" if v >= 0 else "n") + str(abs(v)))
    offset_str = "_" + "".join(offset_strs) if offset_strs else ""
    local_name = f"{prefix}{name}{offset_str}"

    return local_name


class SDFGBuilder:
    class GenerateIterationRangePass(gt_ir.IRNodeVisitor):
        @classmethod
        def apply(cls, iir):
            instance = cls(iir)
            instance.visit(iir)

        def __init__(self, iir):
            self.iir = iir
            self.visitor_state = dict(i_range=None, j_range=None)

        def visit_Stage(self, node: gt_ir.Stage):
            i_extent = node.compute_extent[0]
            j_extent = node.compute_extent[1]
            self.visitor_state["i_range"] = i_extent
            self.visitor_state["j_range"] = j_extent
            # self.visitor_state["i_range"] = "{lb}:I{rb}".format(
            #     lb=i_extent[0], rb=(" + " + str(i_extent[1])) if i_extent[1] else "",
            # )
            # self.visitor_state["j_range"] = "{lb}:J{rb}".format(
            #     lb=str(j_extent[0]), rb=(" + " + str(j_extent[1])) if j_extent[1] else "",
            # )
            self.generic_visit(node)
            self.visitor_state["i_range"] = None
            self.visitor_state["j_range"] = None

        def visit_ApplyBlock(self, node: gt_ir.ApplyBlock):
            node.i_range = self.visitor_state["i_range"]
            node.j_range = self.visitor_state["j_range"]
            k_start = (
                node.interval.start.offset
                if node.interval.start.level == gt_ir.LevelMarker.START
                else "K{:+d}".format(node.interval.start.offset)
            )
            k_end = (
                node.interval.end.offset
                if node.interval.end.level == gt_ir.LevelMarker.START
                else "K{:+d}".format(node.interval.end.offset)
            )
            node.k_range = (k_start, k_end)

            # node.k_range_tuple = (str(k_extent[0]), 'K+'+str(k_extent[1]))
            #     = "K{l}:K{r}".format(
            #     l=(" - " + str(-j_extent[0]) if j_extent[0] else ""),
            #     r=(" + " + str(j_extent[1]) if j_extent[1] else ""),
            # )
            # self.generic_visit(node)

    class GenerateTaskletInfoPass(gt_ir.IRNodeVisitor):
        @classmethod
        def apply(cls, iir: gt_ir.StencilImplementation):
            transormer = cls()
            transormer.visit(iir)

        def generic_visit(self, node: gt_ir.Node, **kwargs):
            res = super().generic_visit(node, **kwargs)
            if res is None:
                return set(), set()
            return res

        def visit_StencilImplementation(self, node: gt_ir.StencilImplementation):
            self.parameters = node.parameters
            self.generic_visit(node)

        def visit_ApplyBlock(self, node: gt_ir.ApplyBlock):
            node.accesses, node.dynamic_accesses = self.visit(node.body)

        def visit_Assign(self, node: gt_ir.Assign, *, accesses):
            value_accesses, value_dynamic_accesses = self.visit(node.value, accesses=accesses)
            target_accesses, target_dynamic_accesses = self.visit(
                node.target, is_target=True, accesses=accesses
            )
            return (
                value_accesses | target_accesses,
                value_dynamic_accesses | target_dynamic_accesses,
            )

        def _visit_branches(self, condition, if_branch, else_branch, **kwargs):

            accesses_condition, accesses_dynamic_condition = self.visit(condition, **kwargs)

            accesses_then, accesses_dynamic_then = self.visit(if_branch, **kwargs)

            accesses_else, accesses_dynamic_else = self.visit(else_branch, **kwargs)

            # accesses are those accessed in any of the branches
            accesses = accesses_condition | accesses_then | accesses_else
            # dynamic accesses are those accessed dynamically in any of the branches...
            accesses_dynamic = (
                accesses_dynamic_condition | accesses_dynamic_then | accesses_dynamic_else
            )
            # ...but not non-dynamically in the condition
            accesses_dynamic -= accesses_condition - accesses_dynamic_condition
            # ...and not non-dynamically in both if- and else branch.
            accesses_dynamic -= (accesses_then - accesses_dynamic_then) & (
                accesses_else - accesses_dynamic_else
            )

            return accesses, accesses_dynamic

        def _visit_stmts(self, stmts, accesses=None):
            if accesses is None:
                accesses = set()
            else:
                accesses = set(accesses)
            accesses_dynamic = set()
            for stmt in stmts:
                accesses_stmt, accesses_dynamic_stmt = self.visit(stmt, accesses=accesses)
                accesses |= accesses_stmt
                accesses_dynamic = (accesses_dynamic_stmt - (accesses - accesses_dynamic)) | (
                    accesses_dynamic - (accesses_stmt - accesses_dynamic_stmt)
                )
            return accesses, accesses_dynamic

        def visit_TernaryOpExpr(self, node: gt_ir.TernaryOpExpr, **kwargs):
            return self._visit_branches(node.condition, node.then_expr, node.else_expr, **kwargs)

        def visit_If(self, node: gt_ir.If, **kwargs):
            return self._visit_branches(node.condition, node.main_body, node.else_body, **kwargs)

        def visit_BlockStmt(self, node: gt_ir.BlockStmt, **kwargs):
            return self._visit_stmts(node.stmts, **kwargs)

        def visit_UnaryOpExpr(self, node: gt_ir.UnaryOpExpr, **kwargs):
            return self.visit(node.arg, **kwargs)

        def visit_BinaryOpExpr(self, node: gt_ir.BinOpExpr, **kwargs):
            return self._visit_stmts([node.lhs, node.rhs], **kwargs)

        def visit_FieldRef(self, node: gt_ir.FieldRef, is_target=False, *, accesses):
            node.was_output = is_target or local_name(node.name, node.offset, True) in accesses
            node.local_name = local_name(node.name, node.offset, node.was_output)

            return {node.local_name}, set()

        def visit_VarRef(self, node: gt_ir.VarRef, is_target=False, *, accesses):
            node.was_output = is_target or local_name(node.name, None, True) in accesses
            node.local_name = local_name(node.name, None, node.was_output)
            return set(), set()

    class GenerateMappedMemletsPass(gt_ir.IRNodeVisitor):
        @classmethod
        def apply(cls, iir: gt_ir.StencilImplementation):
            transformer = cls()
            transformer.input_memlets = None
            transformer.output_memlets = None
            transformer.visit(iir)

        def visit_Assign(self, node: gt_ir.Assign):
            assert isinstance(node.target, (gt_ir.FieldRef, gt_ir.VarRef))
            self.visit(node.value)
            self.visit(node.target)

        def visit_StencilImplementation(self, node: gt_ir.StencilImplementation):
            self.fields = node.fields
            self.field_extents = node.fields_extents
            self.parameters = node.parameters
            self.generic_visit(node)

        def visit_ApplyBlock(self, node: gt_ir.ApplyBlock):
            self.input_memlets = dict()
            self.output_memlets = dict()
            self.ranges = (node.i_range, node.j_range, node.k_range)
            self.apply_block = node
            self.visit(node.body)

            node.mapped_input_memlets = dict()
            node.mapped_output_memlets = dict()
            node.mapped_input_memlet_infos = self.input_memlets
            node.mapped_output_memlet_infos = self.output_memlets

            for memlet_name, memlet_info in self.input_memlets.items():
                node.mapped_input_memlets[memlet_name] = dace.Memlet.simple(
                    memlet_info.outer_name,
                    memlet_info.subset_str,
                    num_accesses=1,
                    dynamic=memlet_info.dynamic,
                )
            for memlet_name, memlet_info in self.output_memlets.items():
                node.mapped_output_memlets[memlet_name] = dace.Memlet.simple(
                    memlet_info.outer_name,
                    memlet_info.subset_str,
                    num_accesses=1,
                    dynamic=memlet_info.dynamic,
                )

            self.input_memlets = None
            self.output_memlets = None
            self.tasklet_targets = None
            self.ranges = None
            self.apply_block = None

        def visit_FieldRef(self, node: gt_ir.FieldRef):
            key = node.local_name
            memlet_dict = self.output_memlets if node.was_output else self.input_memlets
            if key not in memlet_dict:
                subset_list = []
                itervar = dict(I="i", J="j", K="k")
                itervar_idx = dict(I=0, J=1, K=2)
                origin = tuple(abs(v) for v in self.field_extents[node.name].lower_indices)
                for k, v in node.offset.items():
                    if k != "K":
                        subset_list.append(
                            f"{origin[itervar_idx[k]] + self.ranges[itervar_idx[k]][0]}{v:+d}:{k}{origin[itervar_idx[k]] + self.ranges[itervar_idx[k]][1]:+d}{v:+d}"
                        )
                        # subset_list.append("0:{}+{:+d}".format(k, v - origin[itervar_idx[k]]))
                    else:
                        # subset_list.append(itervar[k].upper() + "{:+d}".format(v))
                        subset_list.append(
                            f"{self.ranges[itervar_idx[k]][0]}+{v:+d}:{self.ranges[itervar_idx[k]][1]}+{v:+d}"
                        )
                        # subset_list.append(
                        #     "{}:{}+{:+d}".format(self.k_range[0], self.k_range[1], v)
                        # )
                subset_str = ", ".join(subset_list) if subset_list else "0"

                memlet_dict[key] = MappedMemletInfo(
                    outer_name=node.name,
                    local_name=key,
                    subset_str=subset_str,
                    offset=node.offset,
                    dynamic=key in self.apply_block.dynamic_accesses,
                )

        # def visit_VarRef(self, node: gt_ir.VarRef):
        #     if node.name in self.parameters:
        #         key = node.local_name
        #         memlet_dict = self.output_memlets if node.was_output else self.input_memlets
        #         if key not in memlet_dict:
        #             memlet_dict[key] = MappedMemletInfo(
        #                 num=self.apply_block.access_counts[key],
        #                 outer_name=node.name,
        #                 local_name=key,
        #                 subset_str="0",
        #                 offset=dict(),
        #             )

    class GenerateTaskletSourcePass(gt_ir.IRNodeVisitor):

        OP_TO_PYTHON = {
            **gt_ir.UnaryOperator.IR_OP_TO_PYTHON_SYMBOL,
            **gt_ir.BinaryOperator.IR_OP_TO_PYTHON_SYMBOL,
        }

        @classmethod
        def apply(cls, iir: gt_ir.StencilImplementation):
            transformer = cls()
            transformer.parameters = iir.parameters
            transformer.visit(iir)

        def visit_ApplyBlock(self, node: gt_ir.ApplyBlock):
            self.tasklet_targets = set()
            sources = self.visit(node.body)
            node.tasklet_code = sources.text
            self.tasklet_targets = None

        ###from debug backend
        def visit_Assign(self, node: gt_ir.Assign):
            assert isinstance(node.target, (gt_ir.FieldRef, gt_ir.VarRef))
            lhs = self.visit(node.target)
            rhs = self.visit(node.value)
            source = f"{lhs} = {rhs}"

            return source

        def visit_BlockStmt(self, node: gt_ir.BlockStmt):
            body_sources = gt_text.TextBlock()
            for stmt in node.stmts:
                stmt_source = self.visit(stmt)
                if isinstance(stmt_source, list):
                    body_sources.extend(stmt_source)
                else:
                    body_sources.append(stmt_source)

            return body_sources

        def visit_ScalarLiteral(self, node: gt_ir.ScalarLiteral):
            dtype: gt_ir.DataType = node.data_type
            ctype = dace.dtypes.DTYPE_TO_TYPECLASS[dtype.dtype.type].ctype
            if np.issubdtype(dtype.dtype.type, np.floating):
                return f"{ctype}({node.value})"
            else:
                return str(node.value)

        def visit_VarRef(self, node: gt_ir.VarRef):
            return node.local_name

        def visit_FieldRef(self, node: gt_ir.FieldRef):
            return node.local_name

        def visit_UnaryOpExpr(self, node: gt_ir.UnaryOpExpr):
            fmt = "({})" if isinstance(node.arg, gt_ir.CompositeExpr) else "{}"
            source = "{op} {expr}".format(
                op=self.OP_TO_PYTHON[node.op], expr=fmt.format(self.visit(node.arg))
            )
            return source

        def visit_BinOpExpr(self, node: gt_ir.BinOpExpr):
            lhs_fmt = "({})" if isinstance(node.lhs, gt_ir.CompositeExpr) else "{}"
            rhs_fmt = "({})" if isinstance(node.rhs, gt_ir.CompositeExpr) else "{}"
            source = "{lhs} {op} {rhs}".format(
                lhs=lhs_fmt.format(self.visit(node.lhs)),
                op=self.OP_TO_PYTHON[node.op],
                rhs=rhs_fmt.format(self.visit(node.rhs)),
            )

            return source

        def visit_TernaryOpExpr(self, node: gt_ir.TernaryOpExpr):
            then_fmt = "({})" if isinstance(node.then_expr, gt_ir.CompositeExpr) else "{}"
            else_fmt = "({})" if isinstance(node.else_expr, gt_ir.CompositeExpr) else "{}"
            source = "{then_expr} if {condition} else {else_expr}".format(
                condition=self.visit(node.condition),
                then_expr=then_fmt.format(self.visit(node.then_expr)),
                else_expr=else_fmt.format(self.visit(node.else_expr)),
            )

            return source

        def visit_If(self, node: gt_ir.If):
            body_sources = gt_text.TextBlock()
            body_sources.append("if {condition}:".format(condition=self.visit(node.condition)))
            body_sources.indent()
            for stmt in node.main_body.stmts:
                body_sources.extend(self.visit(stmt))
            body_sources.dedent()
            if node.else_body:
                body_sources.append("else:")
                body_sources.indent()

                for stmt in node.else_body.stmts:
                    body_sources.extend(self.visit(stmt))
                body_sources.dedent()
            return ["".join([str(item) for item in line]) for line in body_sources.lines]

    class GenerateSDFGPass(gt_ir.IRNodeVisitor):
        @classmethod
        def apply(cls, iir: gt_ir.StencilImplementation):
            transformer = cls(iir.name.split(".")[-1])
            transformer.visit(iir)
            return transformer.sdfg

        def __init__(self, name):
            self.sdfg = dace.SDFG(name)
            self.sdfg.instrument = dace.InstrumentationType.Timer

            self.iteration_order = None

            self.tail_state = self.sdfg.add_state(is_start_state=True)
            I = dace.symbol("I")
            J = dace.symbol("J")
            K = dace.symbol("K")
            self.map_counter = itertools.count()
            self.tasklet_counter = itertools.count()
            self.apply_method_counter = itertools.count()
            self.symbolic_domain = (I, J, K)

        def new_tasklet_name(self):
            return "tasklet_" + str(next(self.tasklet_counter))

        def new_stage_name(self):
            return "stencil_" + str(next(self.apply_method_counter))

        def new_map_name(self):
            return "map_" + str(next(self.map_counter))

        def _append_states(self, entry_state, exit_state):
            self.sdfg.add_edge(self.tail_state, entry_state, dace.InterstateEdge())
            self.tail_state = exit_state

        def _make_mapped_computation(self, node: gt_ir.ApplyBlock, map_range):
            state = self.sdfg.add_state()

            tasklet = state.add_tasklet(
                name=self.new_tasklet_name(),
                inputs=node.mapped_input_memlets,
                outputs=node.mapped_output_memlets,
                code=node.tasklet_code,
            )
            map_entry, map_exit = state.add_map(name=self.new_map_name(), ndrange=map_range)

            for memlet_info in node.mapped_input_memlet_infos.values():
                name = memlet_info.outer_name
                state.add_memlet_path(
                    state.add_read(name),
                    map_entry,
                    tasklet,
                    memlet=node.mapped_input_memlets[memlet_info.local_name],
                    dst_conn=memlet_info.local_name,
                )
            if len(node.mapped_input_memlet_infos) == 0:
                state.add_edge(map_entry, None, tasklet, None, dace.EmptyMemlet())

            out_field_accessors = dict()
            for memlet_info in node.mapped_output_memlet_infos.values():
                name = memlet_info.outer_name
                if name not in out_field_accessors:
                    out_field_accessors[name] = state.add_write(name)
                state.add_memlet_path(
                    tasklet,
                    map_exit,
                    out_field_accessors[name],
                    memlet=node.mapped_output_memlets[memlet_info.local_name],
                    src_conn=memlet_info.local_name,
                )
            if len(node.mapped_output_memlet_infos) == 0:
                state.add_edge(tasklet, None, map_exit, None, dace.EmptyMemlet())

            return state

        def visit_FieldRef(self, node: gt_ir.FieldRef):
            import copy
            import dace.data

            if node.name not in self.apply_block.arrays:
                array: dace.data.Array = copy.deepcopy(self.sdfg.arrays[node.name])
                array.transient = False
                self.apply_block.arrays[node.name] = array

        def visit_VarRef(self, node: gt_ir.VarRef):
            if node.name not in self.apply_block.symbols:
                self.apply_block.symbols[
                    local_name(node.name, None, is_target=False)
                ] = dace.symbol(
                    local_name(node.name, None, is_target=False),
                    dace.dtypes.typeclass(self.parameters[node.name].data_type.dtype.type),
                )

        def visit_ApplyBlock(self, node: gt_ir.ApplyBlock):
            node.arrays = {}
            node.symbols = {}
            self.apply_block = node
            self.generic_visit(node)
            self.apply_block = None

            from .library import StencilLibraryNode, ApplyMethod

            state = self.sdfg.add_state()

            label = self.new_stage_name()
            symbols = {}

            apply_method = ApplyMethod(
                label,
                node.k_range,
                arrays=node.arrays,
                read_accesses=node.mapped_input_memlet_infos,
                write_accesses=node.mapped_output_memlet_infos,
                code=node.tasklet_code,
                symbols=node.symbols,
            )
            library_node = StencilLibraryNode(
                name=label,
                iteration_order=self.iteration_order,
                ij_range=(node.i_range, node.j_range),
                intervals=[apply_method]
                # read_accesses=node.mapped_input_memlet_infos,
                # write_accesses=node.mapped_output_memlet_infos,
            )
            state.add_node(library_node)

            input_memlets = dict()
            for input_memlet in node.mapped_input_memlets.values():
                if input_memlet.data in input_memlets:
                    subset = dace.memlet.subsets.union(
                        input_memlet.subset, input_memlets[input_memlet.data].subset
                    )
                    input_memlets[input_memlet.data] = dace.memlet.Memlet.simple(
                        input_memlets[input_memlet.data], subset_str=str(subset)
                    )
                else:
                    input_memlets[input_memlet.data] = input_memlet

            for input_memlet in input_memlets.values():
                state.add_edge(
                    state.add_read(input_memlet.data),
                    None,
                    library_node,
                    "IN_" + input_memlet.data,
                    input_memlet,
                )

            output_memlets = dict()
            for output_memlet in node.mapped_output_memlets.values():
                if output_memlet.data in output_memlets:
                    subset = dace.memlet.subsets.union(
                        output_memlet.subset, output_memlets[output_memlet.data].subset
                    )
                    output_memlets[output_memlet.data] = dace.memlet.Memlet.simple(
                        output_memlets[output_memlet.data], subset_str=str(subset)
                    )
                else:
                    output_memlets[output_memlet.data] = output_memlet
            for output_memlet in output_memlets.values():
                state.add_edge(
                    library_node,
                    "OUT_" + output_memlet.data,
                    state.add_write(output_memlet.data),
                    None,
                    output_memlet,
                )

            self._append_states(state, state)

        def visit_MultiStage(self, node: gt_ir.MultiStage):
            self.iteration_order = node.iteration_order
            self.generic_visit(node)
            self.iteration_order = None

        def visit_StencilImplementation(self, node: gt_ir.StencilImplementation):
            self.fields = node.fields
            self.parameters = node.parameters
            for field in node.fields.values():
                if field.name in node.unreferenced:
                    continue
                shape = [
                    d + f
                    for d, f in zip(
                        self.symbolic_domain, node.fields_extents[field.name].frame_size
                    )
                ]
                if field.name in node.arg_fields:
                    I_stride = dace.symbol(f"_{field.name}_I_stride")
                    J_stride = dace.symbol(f"_{field.name}_J_stride")
                    K_stride = dace.symbol(f"_{field.name}_K_stride")
                    self.sdfg.add_array(
                        field.name,
                        strides=(I_stride, J_stride, K_stride),
                        shape=shape,
                        dtype=field.data_type.dtype.type,
                    )
                else:
                    assert field.name in node.temporary_fields

                    self.sdfg.add_transient(
                        field.name,
                        shape=shape,
                        dtype=field.data_type.dtype.type,
                        lifetime=dace.dtypes.AllocationLifetime.Persistent,
                    )
            for k, v in node.parameters.items():
                self.sdfg.add_symbol(local_name(k, None, False), v.data_type.dtype.type)
            self.generic_visit(node)

    @classmethod
    def apply(cls, iir):
        cls.GenerateIterationRangePass.apply(iir)
        cls.GenerateTaskletInfoPass.apply(iir)
        cls.GenerateMappedMemletsPass.apply(iir)
        cls.GenerateTaskletSourcePass.apply(iir)
        sdfg = cls.GenerateSDFGPass.apply(iir)
        return sdfg
