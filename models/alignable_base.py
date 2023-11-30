import json
import numpy as np
from typing import List, Optional, Tuple, Union, Dict

from models.utils import *
import models.interventions
from models.constants import CONST_QKV_INDICES

from torch import optim
from transformers import (
    get_linear_schedule_with_warmup
)
from tqdm import tqdm, trange

        
class AlignableModel(nn.Module):
    """
    Generic alignable model. Alignments are specified in the config.
    """
    

    def __init__(
        self, 
        alignable_config,
        model
    ):
        super().__init__()
        self.mode = alignable_config.mode
        intervention_type = alignable_config.alignable_interventions_type

        # each representation can get a different intervention type
        if type(intervention_type) == list:
            assert len(intervention_type) == len(alignable_config.alignable_representations)
            assert all([issubclass(t, models.interventions.Intervention) for t in intervention_type])
        
        ###
        # We instantiate intervention_layers at locations.
        # Note that the layer name mentioned in the config is
        # abstract. Not the actual module name of the model.
        # 
        # This script will automatically convert abstract
        # name into module name if the model type is supported.
        #
        # To support a new model type, you need to provide a
        # mapping between supported abstract type and module name.
        ###
        self.alignable_representations = {}
        self.interventions = {}
        self._key_collision_counter = {}
        # Flags and counters below are for interventions in the model.generate
        # call. We can intervene on the prompt tokens only, on each generated
        # token, or on a combination of both.
        self._is_generation = False
        self._intervene_on_prompt = None
        self._key_getter_call_counter = {}
        self._key_setter_call_counter = {}
        
        # In case interventions sharing weights, we need to partition subspace as well.
        self._intervention_shared_weights = {}
        self._intervention_shared_weights_dimension_occupancy = {}
        
        for i, representation in enumerate(alignable_config.alignable_representations):
            intervention_function = intervention_type if type(intervention_type) != list else intervention_type[i]
            intervention = intervention_function(
                get_alignable_dimension(get_internal_model_type(model), model.config, representation),
                proj_dim=representation.alignable_low_rank_dimension,
                # we can partition the subspace, and intervene on subspace
                subspace_partition=representation.subspace_partition
            )
            alignable_module_hook = get_alignable_module_hook(model, representation)
            
            _key = self._get_representation_key(representation)
            self.alignable_representations[_key] = representation
            self.interventions[_key] = (intervention, alignable_module_hook)
            self._key_getter_call_counter[_key] = 0 # we memo how many the hook is called, 
                                                    # usually, it's a one time call per 
                                                    # hook unless model generates.
            self._key_setter_call_counter[_key] = 0
        self.sorted_alignable_keys = sort_alignables_by_topological_order(
            model,
            self.alignable_representations
        )
        
        # model with cache activations
        self.activations = {}
        """
        Activations in the future list is ALWAYS causally before
        the vanilla activation list. This field becomes crucial
        if we intervene at the same place multiple times.
        """
        self.model = model
        self.model_config = model.config
        self.model_type = get_internal_model_type(model)
        self.disable_model_gradients()
        

    def __str__(self):
        """
        Print out basic info about this alignable instance
        """
        attr_dict = {
            "model_type": self.model_type,
            "alignable_interventions_type": self.alignable_interventions_type,
            "alignabls": self.sorted_alignable_keys,
            "mode": self.mode
        }
        return json.dumps(attr_dict, indent=4)


    def _get_representation_key(self, representation):
        """
        Provide unique key for each intervention
        """
        l = representation.alignable_layer
        r = representation.alignable_representation_type
        u = representation.alignable_unit
        n = representation.max_number_of_units
        key_proposal = f"layer.{l}.repr.{r}.unit.{u}.nunit.{n}"
        if key_proposal not in self._key_collision_counter:
            self._key_collision_counter[key_proposal] = 0
        else:
            self._key_collision_counter[key_proposal] += 1
        return f"{key_proposal}#{self._key_collision_counter[key_proposal]}"
    
    
    def _reset_hook_count(self):
        """
        Reset the hook count before any generate call
        """
        self._key_getter_call_counter = dict.fromkeys(
            self._key_getter_call_counter, 0)
        self._key_setter_call_counter = dict.fromkeys(
            self._key_setter_call_counter, 0)

    
    def _remove_forward_hooks(self):
        """
        Clean up all the remaining hooks before any call
        """
        remove_forward_hooks(self.model)
    
    
    def _cleanup_states(self):
        """
        Clean up all old in memo states of interventions
        """
        self._is_generation = False
        self._remove_forward_hooks()
        self._reset_hook_count()
        self.activations.clear()
    
    
    def get_trainable_parameters(self):
        """
        Return trainable params as key value pairs
        """
        ret_params = []
        for k, v in self.interventions.items():
            if isinstance(
                v[0], 
                models.interventions.TrainbleIntervention
            ):
                ret_params += [p for p in v[0].parameters()]
        return ret_params
    

    def get_cached_activations(self):
        """
        Return the cached activations with keys
        """
        return self.activations
                
    
    def set_temperature(self, temp: torch.Tensor):
        """
        Set temperature if needed
        """
        for k, v in self.interventions.items():
            if isinstance(
                v[0], 
                models.interventions.BoundlessRotatedSpaceIntervention
            ):
                v[0].set_temperature(temp)
    
    
    def disable_model_gradients(self):
        """
        Disable gradient in the model
        """
        # Freeze all model weights
        self.model.eval()
        for param in self.model.parameters():
            param.requires_grad = False
            

    def disable_intervention_gradients(self):
        """
        Disable gradient in the trainable intervention
        """
        # Freeze all intervention weights
        pass
        
    
    def set_device(self, device):
        """
        Set device of interventions and the model
        """
        for k, v in self.interventions.items():
            if isinstance(
                v[0], 
                models.interventions.TrainbleIntervention
            ):
                v[0].to(device)
        self.model.to(device)

        
    def get_device(self):
        """
        Get device of interventions and the model
        """
        return self.model.device
        
        
    def count_parameters(self):
        """
        Set device of interventions and the model
        """
        total_parameters = 0
        for k, v in self.interventions.items():
            if isinstance(
                v[0], 
                models.interventions.TrainbleIntervention
            ):
                total_parameters += count_parameters(v[0])
        return total_parameters       
        
        
    def set_zero_grad(self):
        """
        Set device of interventions and the model
        """
        for k, v in self.interventions.items():
            if isinstance(
                v[0], 
                models.interventions.TrainbleIntervention
            ):
                v[0].zero_grad()

    
    def _gather_intervention_output(
        self, output,
        alignable_representations_key,
        unit_locations
    ) -> torch.Tensor:
        """
        Gather intervening activations from the output based on indices
        """
        original_output = output
        # data structure casting
        if isinstance(output, tuple):
            original_output = output[0]
        # gather subcomponent
        original_output = self._output_to_subcomponent(
            original_output,
            alignable_representations_key
        )
        # gather based on intervention locations
        selected_output = gather_neurons(
            original_output,
            self.alignable_representations[
                alignable_representations_key].alignable_unit,
            unit_locations
        )
        return selected_output


    def _output_to_subcomponent(
        self, output, alignable_representations_key,
    ) -> List[torch.Tensor]:
        """
        Helps to get subcomponent of inputs/outputs of a hook
        
        For instance, we need to separate QKV from a hidden representation
        by slicing the original output
        """
        return output_to_subcomponent(
            output, 
            self.alignable_representations[
                alignable_representations_key
            ].alignable_representation_type, 
            self.model_type,
            self.model_config
        )

    
    def _scatter_intervention_output(
        self, output, intervened_representation,
        alignable_representations_key,
        unit_locations
    ) -> torch.Tensor:
        """
        Scatter in the intervened activations in the output
        """
        original_output = output
        # data structure casting
        if isinstance(output, tuple):
            original_output = output[0]
        
        alignable_representation_type = self.alignable_representations[
            alignable_representations_key
        ].alignable_representation_type
        alignable_unit = self.alignable_representations[
            alignable_representations_key
        ].alignable_unit
            
        replaced_output = scatter_neurons(
            original_output, 
            intervened_representation, 
            alignable_representation_type,
            alignable_unit,
            unit_locations, 
            self.model_type,
            self.model_config
        )
        return replaced_output
    

    def _intervention_getter(
        self, alignable_keys, unit_locations,
    ) -> HandlerList:  
        """
        Create a list of getter handlers that will fetch activations
        """
        handlers = []
        for key_i, key in enumerate(alignable_keys):
            _, alignable_module_hook = self.interventions[key]
            def hook_callback(model, args, kwargs, output=None):
                if self._is_generation:
                    is_prompt = self._key_getter_call_counter[key] == 0
                    if not self._intervene_on_prompt or is_prompt:
                        self._key_getter_call_counter[key] += 1
                    if self._intervene_on_prompt ^ is_prompt:
                        return  # no-op
                arg_ptr = None
                if output is None:
                    if len(args) == 0: # kwargs based calls
                        # PR: https://github.com/frankaging/align-transformers/issues/11
                        # We cannot assume the dict only contain one element
                        arg_ptr = kwargs[list(kwargs.keys())[0]]
                    else:
                        arg_ptr = args
                else:
                    arg_ptr = output

                selected_output = self._gather_intervention_output(
                    arg_ptr, key, unit_locations[key_i]
                )
                self.activations[key] = selected_output
            handlers.append(alignable_module_hook(hook_callback, with_kwargs=True))

        return HandlerList(handlers)
    
        
    def _intervention_setter(
        self, alignable_keys, unit_locations_source, 
        unit_locations_base, subspaces,
    ) -> HandlerList: 
        """
        Create a list of setter handlers that will set activations
        """
        handlers = []
        for key_i, key in enumerate(alignable_keys):
            intervention, alignable_module_hook = self.interventions[key]
            def hook_callback(model, args, kwargs, output=None):
                if self._is_generation:
                    is_prompt = self._key_setter_call_counter[key] == 0
                    if not self._intervene_on_prompt or is_prompt:
                        self._key_setter_call_counter[key] += 1
                    if self._intervene_on_prompt ^ is_prompt:
                        return  # no-op
                if output is None:
                    arg_ptr = None
                    if len(args) == 0: # kwargs based calls
                        # PR: https://github.com/frankaging/align-transformers/issues/11
                        # We cannot assume the dict only contain one element
                        arg_ptr = kwargs[list(kwargs.keys())[0]]
                    else:
                        arg_ptr = args
                    # intervene in the module input with a pre forward hook
                    selected_output = self._gather_intervention_output(
                        arg_ptr, 
                        key, unit_locations_base[key_i]
                    )
                    # intervene with cached activations
                    intervened_representation = do_intervention(
                        selected_output, self.activations[key], 
                        intervention, 
                        subspaces[key_i] if subspaces is not None else None,
                    )
                    # patched in the intervned activations
                    arg_ptr = self._scatter_intervention_output(
                        arg_ptr, intervened_representation,
                        key, unit_locations_base[key_i]
                    )
                else:
                    selected_output = self._gather_intervention_output(
                        output, key, unit_locations_base[key_i]
                    )
                    # intervene with cached activations
                    intervened_representation = do_intervention(
                        selected_output, self.activations[key], 
                        intervention, 
                        subspaces[key_i] if subspaces is not None else None,
                    )
                    # patched in the intervned activations
                    output = self._scatter_intervention_output(
                        output, intervened_representation,
                        key, unit_locations_base[key_i]
                    )
            handlers.append(alignable_module_hook(hook_callback, with_kwargs=True))
            
        return HandlerList(handlers)
        
    
    def forward(
        self, 
        base,
        sources: Optional[List] = None,
        unit_locations: Optional[Dict] = None,
        activations_sources: Optional[Dict] = None,
        subspaces: Optional[List] = None,
    ):
        """
        Main forward function that serves a wrapper to
        actual model forward calls. It will use forward
        hooks to do interventions.

        In essense, sources will lead to getter hooks to
        get activations. We will use these activations to
        intervene on our base example.

        Parameters:
        base:                The base example.
        sources:             A list of source examples.
        unit_locations:      The intervention locations.
        activations_sources: A list of representations.
        subspace:            Subspace interventions.
        
        Return:
        base_output: the non-intervened output of the base
        input.
        counterfactual_outputs: the intervened output of the
        base input.
        
        Notes:
        
        1) unit_locations
        unit_locations is a dict where keys are tied with
        example pairs involved in one intervention as,
        {
            "sources->base" : List[]
        }
        
        the shape can be
        
        2 * num_intervention * bs * num_max_unit
        
        OR
        
        2 * num_intervention * num_intervention_level * bs * num_max_unit
        
        if we intervene on h.pos which is a nested intervention location.
        
        2) subspaces
        subspaces is a list of indices indicating which subspace will
        this intervention target given an example in the batch. 
        
        An intervention could be initialized with subspace parition as,
        [[... subspace_1 ...], [... subspace_2 ...], [rest]]
        
        An intervention may be targeting a specific partition.
        
        This input field should look like something like,
        [
            [[subspace indices], [subspace indices]], <- for the first intervention
            None,                                     <- for the second intervention
            [[subspace indices], [subspace indices]]
        ]
        
        Only setter (where do_intervention is called) needs this field.
        
        *We assume base and source targetting the same subspace for now.
        *We assume only a single space is targeted for now (although 2d list is provided).
        """
        self._cleanup_states()
        
        # if no source inputs, we are calling a simple forward
        if sources is None and activations_sources is None:
            return self.model(**base), None
        
        if sources is not None:
            assert len(sources) == len(self.sorted_alignable_keys)
        else:
            assert len(activations_sources) == len(self.sorted_alignable_keys)
            
        if self.mode == "parallel":
            assert "sources->base" in unit_locations
            unit_locations_sources = unit_locations["sources->base"][0]
            unit_locations_base = unit_locations["sources->base"][1]
        elif activations_sources is None and self.mode == "serial":
            assert "sources->base" not in unit_locations
            assert len(sources) == len(unit_locations)
            
        batch_size = base["input_ids"].shape[0]
        device = base["input_ids"].device
        # returning un-intervened output without gradients
        with torch.inference_mode():
            base_outputs = self.model(**base)
        
        all_set_handlers = HandlerList([])
        if self.mode == "parallel":
            # for each source, we hook in getters to cache activations
            # at each aligning representations
            if activations_sources is None:
                for key_i, alignable_key in enumerate(self.sorted_alignable_keys):
                    get_handlers = self._intervention_getter(
                        [alignable_key],
                        [unit_locations_sources[key_i]],
                    )
                    _ = self.model(**sources[key_i])
                    get_handlers.remove()
            else:
                # simply patch in the ones passed in
                self.activations = activations_sources
                for key_i, alignable_key in enumerate(self.sorted_alignable_keys):
                    assert alignable_key in self.activations
                
            # in parallel mode, we swap cached activations all into
            # base at once
            for key_i, alignable_key in enumerate(self.sorted_alignable_keys):
                set_handlers = self._intervention_setter(
                    [alignable_key],
                    [unit_locations_sources[key_i]],
                    [unit_locations_base[key_i]],
                    [subspaces[key_i]] if subspaces is not None else None,
                )
                # for setters, we don't remove them.
                all_set_handlers.extend(set_handlers)
            counterfactual_outputs = self.model(**base)
            all_set_handlers.remove()
            
        elif self.mode == "serial":
            for key_i, alignable_key in enumerate(self.sorted_alignable_keys):
                if key_i != len(self.sorted_alignable_keys)-1:
                    unit_locations_key = f"source_{key_i}->source_{key_i+1}"
                else:
                    unit_locations_key = f"source_{key_i}->base"

                unit_locations_source = \
                    unit_locations[unit_locations_key][0][0] # last one as only one intervention
                                                             # per source in serial case
                unit_locations_base = \
                    unit_locations[unit_locations_key][1][0]
                
                if activations_sources is None:
                    # get activation from source_i
                    get_handlers = self._intervention_getter(
                        [alignable_key],
                        [unit_locations_source],
                    )
                    _ = self.model(**sources[key_i]) # this is when previous setter and THEN the getter get called
                    get_handlers.remove()
                    # remove existing setters after getting the curr intervened reprs
                    if len(all_set_handlers) > 0:
                        all_set_handlers.remove()
                        all_set_handlers = HandlerList([])
                else:
                    self.activations[alignable_key] = activations_sources[alignable_key]
                    
                # set with intervened activation to source_i+1
                set_handlers = self._intervention_setter(
                    [alignable_key],
                    [unit_locations_source],
                    [unit_locations_base],
                    [subspaces[key_i]] if subspaces is not None else None,
                )
                # for setters, we don't remove them.
                all_set_handlers.extend(set_handlers)
            counterfactual_outputs = self.model(**base)
            all_set_handlers.remove()
                
        return base_outputs, counterfactual_outputs
    

    def generate(
        self, 
        base,
        sources: Optional[List] = None,
        unit_locations: Optional[Dict] = None,
        activations_sources: Optional[Dict] = None,
        intervene_on_prompt: bool = True,
        **kwargs
    ):
        """
        Intervenable generation function that serves a
        wrapper to regular model generate calls.

        Currently, we support basic interventions **in the
        prompt only**. We will support generation interventions
        in the next release.
        
        TODO: Unroll sources and intervene in the generation step.

        Parameters:
        base:                The base example.
        sources:             A list of source examples.
        unit_locations:      The intervention locations of
                             base.
        activations_sources: A list of representations.
        intervene_on_prompt: Whether only intervene on prompt.
        **kwargs:            All other generation parameters.
        
        Return:
        base_output: the non-intervened output of the base
        input.
        counterfactual_outputs: the intervened output of the
        base input.
        """
        self._cleanup_states()
        
        # if no source inputs, we are calling a simple forward
        print("WARNING: This is a basic version that will "
              "intervene on some of the prompt token as well as "
              "the each generation step."
             )
        self._intervene_on_prompt = intervene_on_prompt
        self._is_generation = True
        
        if sources is None and activations_sources is None:
            return self.model.generate(
                inputs=base["input_ids"],
                **kwargs
            ), None
        
        if sources is not None:
            assert len(sources) == len(self.sorted_alignable_keys)
        else:
            assert len(activations_sources) == len(self.sorted_alignable_keys)
            
        if self.mode == "parallel":
            assert "sources->base" in unit_locations
            unit_locations_sources = unit_locations["sources->base"][0]
            unit_locations_base = unit_locations["sources->base"][1]
        elif activations_sources is None and self.mode == "serial":
            assert "sources->base" not in unit_locations
            assert len(sources) == len(unit_locations)
            
        batch_size = base["input_ids"].shape[0]
        device = base["input_ids"].device
        # returning un-intervened output without gradients
        with torch.inference_mode():
            base_outputs = self.model.generate(
                inputs=base["input_ids"],
                **kwargs
            )
        
        all_set_handlers = HandlerList([])
        if self.mode == "parallel":
            # for each source, we hook in getters to cache activations
            # at each aligning representations
            if activations_sources is None:
                for key_i, alignable_key in enumerate(self.sorted_alignable_keys):
                    get_handlers = self._intervention_getter(
                        [alignable_key],
                        [unit_locations_sources[key_i]],
                    )
                    _ = self.model(**sources[key_i])
                    get_handlers.remove()
            else:
                # simply patch in the ones passed in
                self.activations = activations_sources
                for key_i, alignable_key in enumerate(self.sorted_alignable_keys):
                    assert alignable_key in self.activations
                
            # in parallel mode, we swap cached activations all into
            # base at once
            for key_i, alignable_key in enumerate(self.sorted_alignable_keys):
                set_handlers = self._intervention_setter(
                    [alignable_key],
                    [unit_locations_sources[key_i]],
                    [unit_locations_base[key_i]],
                    [subspaces[key_i]] if subspaces is not None else None,
                )
                # for setters, we don't remove them.
                all_set_handlers.extend(set_handlers)
            counterfactual_outputs = self.model.generate(
                inputs=base["input_ids"],
                **kwargs
            )
            all_set_handlers.remove()
            
        elif self.mode == "serial":
            for key_i, alignable_key in enumerate(self.sorted_alignable_keys):
                if key_i != len(self.sorted_alignable_keys)-1:
                    unit_locations_key = f"source_{key_i}->source_{key_i+1}"
                else:
                    unit_locations_key = f"source_{key_i}->base"

                unit_locations_source = \
                    unit_locations[unit_locations_key][0][0] # last one as only one intervention
                                                             # per source in serial case
                unit_locations_base = \
                    unit_locations[unit_locations_key][1][0]
                
                if activations_sources is None:
                    # get activation from source_i
                    get_handlers = self._intervention_getter(
                        [alignable_key],
                        [unit_locations_source],
                    )
                    _ = self.model(**sources[key_i]) # this is when previous setter and THEN the getter get called
                    get_handlers.remove()
                    # remove existing setters after getting the curr intervened reprs
                    if len(all_set_handlers) > 0:
                        all_set_handlers.remove()
                        all_set_handlers = HandlerList([])
                else:
                    self.activations[alignable_key] = activations_sources[alignable_key]
                    
                # set with intervened activation to source_i+1
                set_handlers = self._intervention_setter(
                    [alignable_key],
                    [unit_locations_source],
                    [unit_locations_base],
                    [subspaces[key_i]] if subspaces is not None else None,
                )
                # for setters, we don't remove them.
                all_set_handlers.extend(set_handlers)
            counterfactual_outputs = self.model.generate(
                inputs=base["input_ids"],
                **kwargs
            )
            all_set_handlers.remove()
        self._is_generation = False
        return base_outputs, counterfactual_outputs
    

    def _batch_process_unit_location(
        self,
        inputs
    ):
        """
        Convert original data batch according
        to the alignable settings.

        The function respects inputs in the following
        data format.


        Each location list in the raw input as,

        [[i, j, ...], [m, n, ...], ...] batched
        where i, j are the unit index, the outter
        list is for the batch


        Possible fields in the input:

        inputs["source_0->base.0.pos"] -> batched
        inputs["source_0->base.1.pos"] -> batched
        AND
        inputs["source_0->source_1.0.pos"] -> batched
        inputs["source_0->source_1.1.pos"] -> batched
        ...

        multiple source locations are included in case
        there are multiple sources.

        We also need to consider whether we are doing
        parallel or serial interventions.

        We also need to consider the granularity. In case
        we are intervening h.pos, which is a specific location
        in a specific head:

        inputs["source_0->base.0.pos"] -> batched
        inputs["source_0->source_1.0.h"] -> batched

        inputs["source_0->base.0.pos"] -> batched
        inputs["source_0->source_1.0.pos"] -> batched
        """
        batched_location_dict = {}


        _source_ind = []
        for k, _ in inputs.items():
            if "->" in k:
                for sub_k in k.split("->"):
                    if "source" in sub_k:
                        _source_ind += [int(sub_k.split("_")[1])]
        _max_source_ind = max(_source_ind)

        # we assume source_0 -> source_1, ..., source_last -> base
        # each pair uses an intervention

        if self.mode == "parallel":
            # all source into base at once but may engage different locations
            _curr_source_ind = 0
            _parallel_aggr_left = []
            _parallel_aggr_right = []
            for _, rep in self.alignable_representations.items():
                _curr_source_ind_inc = _curr_source_ind + 1
                _prefix = f"source_{_curr_source_ind}->base"
                _prefix_left = f"{_prefix}.0"
                _prefix_right = f"{_prefix}.1"
                _sub_loc_aggr_left = [] # 3d
                _sub_loc_aggr_right = [] # 3d
                for sub_loc in rep.alignable_unit.split("."):
                    _sub_loc_aggr_left += [
                        inputs[f"{_prefix_left}.{sub_loc}"]]
                    _sub_loc_aggr_right += [
                        inputs[f"{_prefix_right}.{sub_loc}"]]
                if len(rep.alignable_unit.split(".")) == 1:
                    _sub_loc_aggr_left = _sub_loc_aggr_left[0]
                    _sub_loc_aggr_right = _sub_loc_aggr_right[0]
                _parallel_aggr_left += [_sub_loc_aggr_left] # 3D or 4D
                _parallel_aggr_right += [_sub_loc_aggr_right] # 3D or 4D
                _curr_source_ind += 1

            batched_location_dict["sources->base"] = (
                _parallel_aggr_left,
                _parallel_aggr_right
            )

        else:
            # source into another source and finally to the base engaging different locations
            _curr_source_ind = 0
            for _, rep in self.alignable_representations.items():
                _curr_source_ind_inc = _curr_source_ind + 1
                _prefix = f"source_{_curr_source_ind}->base" if _curr_source_ind+1 == \
                    len(self.alignable_representations) else \
                    f"source_{_curr_source_ind}->source{_curr_source_ind_inc}"
                _prefix_left = f"{_prefix}.0"
                _prefix_right = f"{_prefix}.1"
                _sub_loc_aggr_left = [] # 3d
                _sub_loc_aggr_right = [] # 3d
                for sub_loc in rep.alignable_unit.split("."):
                    _sub_loc_aggr_left += [
                        inputs[f"{_prefix_left}.{sub_loc}"]]
                    _sub_loc_aggr_right += [
                        inputs[f"{_prefix_right}.{sub_loc}"]]
                if len(rep.alignable_unit.split(".")) == 1:
                    _sub_loc_aggr_left = _sub_loc_aggr_left[0]
                    _sub_loc_aggr_right = _sub_loc_aggr_right[0]
                _curr_source_ind += 1
                batched_location_dict[_prefix] = (
                    [_sub_loc_aggr_left], # 3D or 4D
                    [_sub_loc_aggr_right] # 3D or 4D
                )

        return batched_location_dict

    
    def find_alignment(
        self,
        train_dataloader,
        compute_loss,
        compute_metrics,
        inputs_collator,
        **kwargs,
    ):
        """
        The method find alignment.
        
        a.k.a. training the intervention

        Notes:
        1) we use Adam, and linear lr scheduling.
        2) you can pass in lr or using default 1e-3
        """
        # preprocess basic kwargs
        lr = kwargs["lr"] if "lr" in kwargs else 1e-3
        epochs = kwargs["epochs"] if "epochs" in kwargs else 10
        warm_up_steps = kwargs["warm_up_steps"] if "warm_up_steps" in kwargs else 0.1
        gradient_accumulation_steps = kwargs["gradient_accumulation_steps"] \
            if "gradient_accumulation_steps" in kwargs else 1

        # some deeper kwargs
        t_total = int(len(train_dataloader) * epochs)
        warm_up_steps = 0.1 * t_total
        target_total_step = len(train_dataloader) * epochs
        optimizer_params = [
            {'params': self.get_trainable_parameters()}
        ]
        optimizer = kwargs["optimizer"] if "optimizer" in kwargs else \
            optim.Adam(optimizer_params, lr=lr)           
        scheduler = kwargs["scheduler"] if "scheduler" in kwargs else \
            get_linear_schedule_with_warmup(optimizer, num_warmup_steps=warm_up_steps,
                num_training_steps=t_total)

        # in case we need additional temp scheduling
        temperature_start = 50.0
        temperature_end = 0.1
        temperature_schedule = torch.linspace(
            temperature_start, temperature_end, target_total_step
        ).to(torch.bfloat16).to(self.get_device())

        # train main loop
        remove_forward_hooks(self.model)
        self.model.eval() # train enables drop-off but no grads
        epoch_iterator = trange(
            0, int(epochs), desc="Epoch"
        )
        total_step = 0
        for epoch in epoch_iterator:
            for step, inputs in enumerate(train_dataloader):
                if inputs_collator is not None:
                    inputs = inputs_collator(inputs)
                b_s = inputs["input_ids"].shape[0]
                unit_location_dict = self._batch_process_unit_location(
                    inputs
                )
                _, counterfactual_outputs = self(
                    {"input_ids": inputs["input_ids"]},
                    [{"input_ids": inputs["source_input_ids"]}],
                    unit_location_dict
                )
                eval_metrics = compute_metrics(
                    [counterfactual_outputs.logits], [inputs['labels']]
                )

                # loss and backprop
                loss = compute_loss(
                    counterfactual_outputs.logits, inputs["labels"]
                )
                loss_str = round(loss.item(), 2)
                epoch_iterator.set_postfix({'loss': loss_str, 'acc': eval_metrics})

                if gradient_accumulation_steps > 1:
                    loss = loss / gradient_accumulation_steps
                if total_step % gradient_accumulation_steps == 0:
                    if not (gradient_accumulation_steps > 1 and total_step == 0):
                        loss.backward()
                        optimizer.step()
                        scheduler.step()
                        self.set_zero_grad()
                        self.set_temperature(temperature_schedule[total_step])
                total_step += 1


    def evaluate_alignment(
        self,
        eval_dataloader,
        compute_metrics,
        inputs_collator,
        **kwargs,
    ):
        """
        The method evaluate alignment.
        """

        all_metrics = []
        all_num_examples = []
        torch.cuda.empty_cache()
        with torch.no_grad():
            for inputs in tqdm(eval_dataloader, desc="Evaluating", leave=False):
                if inputs_collator is not None:
                    inputs = inputs_collator(inputs)
                b_s = inputs["input_ids"].shape[0]
                unit_location_dict = self._batch_process_unit_location(
                    inputs,
                )
                _, counterfactual_outputs = self(
                    {"input_ids": inputs["input_ids"]},
                    [{"input_ids": inputs["source_input_ids"]}],
                    unit_location_dict
                )
                eval_metrics = compute_metrics(
                    [counterfactual_outputs.logits], [inputs['labels']]
                )
                all_metrics += [eval_metrics]
                all_num_examples += [b_s]
        result = weighted_average(all_metrics, all_num_examples)

        return result
    