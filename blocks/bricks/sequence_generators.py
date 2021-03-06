"""Sequence generation framework."""
from abc import ABCMeta, abstractmethod

from six import add_metaclass
from theano import tensor

from blocks.bricks import Initializable, Identity, MLP, Random
from blocks.bricks.base import application, Brick, lazy
from blocks.bricks.parallel import Fork, Distribute
from blocks.bricks.lookup import LookupTable
from blocks.bricks.recurrent import recurrent
from blocks.bricks.attention import (
    AbstractAttentionRecurrent, AttentionRecurrent)
from blocks.utils import dict_union


class BaseSequenceGenerator(Initializable):
    """A generic sequence generator.

    This class combines two components, a readout network and an
    attention-equipped recurrent transition, into a context-dependent
    sequence generator. Optionally a third component can be used which
    forks feedback from the readout network to obtain inputs for the
    transition.

    **Definitions:**

    * *States* of the generator are the states of the transition as
      specified in `transition.state_names`.

    * *Contexts* of the generator are the contexts of the transition as
      specified in `transition.context_names`.

    * *Glimpses* are intermediate entities computed at every generation
      step from states, contexts and the previous step glimpses. They are
      computed in the transition's `apply` method when not given or by
      explicitly calling the transition's `take_glimpses` method. The set
      of glimpses considered is specified in `transition.glimpse_names`.

    The generation algorithm description follows.

    **Algorithm:**

    1. The initial states are computed from the contexts. This includes
       fake initial outputs given by the `initial_outputs` method of the
       readout, initial states and glimpses given by the `initial_state`
       method of the transition.

    2. Given the contexts, the current state and the glimpses from the
       previous step the attention mechanism hidden in the transition
       produces current step glimpses. This happens in the `take_glimpses`
       method of the transition.

    3. Using the contexts, the fed back output from the previous step, the
       current states and glimpses, the readout brick is used to generate
       the new output by calling its `readout` and `emit` methods.

    4. The new output is fed back in the `feedback` method of the readout
       brick. This feedback, together with the contexts, the glimpses and
       the previous states is used to get the new states in the
       transition's `apply` method. Optionally the `fork` brick is used in
       between to compute the transition's inputs from the feedback.

    5. Back to step 1 if desired sequence length is not yet reached.

    | A scheme of the algorithm described above follows.

    .. image:: /_static/sequence_generator_scheme.png
            :height: 500px
            :width: 500px

    ..

    **Notes:**

    * For machine translation we would have only one glimpse: the weighted
      average of the annotations.

    * For speech recognition we would have three: the weighted average,
      the alignment and the monotonicity penalty.

    Parameters
    ----------
    readout : instance of :class:`AbstractReadout`
        The readout component of the sequence generator.
    transition : instance of :class:`AbstractAttentionRecurrent`
        The transition component of the sequence generator.
    fork : :class:`.Brick`
        The brick to compute the transition's inputs from the feedback.

    Notes
    -----
    See :class:`.Initializable` for initialization parameters.

    """
    @lazy
    def __init__(self, readout, transition, fork=None, **kwargs):
        super(BaseSequenceGenerator, self).__init__(**kwargs)
        self.readout = readout
        self.transition = transition
        self.fork = fork

        self.state_names = transition.compute_states.outputs
        self.context_names = transition.apply.contexts
        self.glimpse_names = transition.take_glimpses.outputs
        self.children = [self.readout, self.fork, self.transition]

    def _push_allocation_config(self):
        # Configure readout
        # TODO: optional states? contexts?
        state_dims = {name: self.transition.get_dim(name)
                      for name in self.state_names}
        context_dims = {name: self.transition.get_dim(name)
                        for name in self.context_names}
        self.glimpse_dims = {name: self.transition.get_dim(name)
                             for name in self.glimpse_names}
        self.readout.source_dims = dict_union(
            state_dims, context_dims, self.glimpse_dims,
            feedback=self.readout.get_dim('feedback'))

        # Configure fork
        feedback_names = self.readout.feedback.outputs
        if not len(feedback_names) == 1:
            raise ValueError
        self.fork.input_dim = self.readout.get_dim(feedback_names[0])
        self.fork.output_dims = {
            name: self.transition.get_dim(name)
            for name in self.fork.apply.outputs}

    @application
    def cost(self, application_call, outputs, mask=None, **kwargs):
        """Returns generation costs for output sequences.

        Parameters
        ----------
        outputs : :class:`~tensor.TensorVariable`
            The 3(2) dimensional tensor containing output sequences.
            The dimension 0 must stand for time, the dimension 1 for the
            position on the batch.
        mask : :class:`~tensor.TensorVariable`
            The binary matrix identifying fake outputs.

        Notes
        -----
        The contexts are expected as keyword arguments.

        """
        batch_size = outputs.shape[-2]  # TODO Assumes only 1 features dim

        # Prepare input for the iterative part
        states = {name: kwargs[name] for name in self.state_names
                  if name in kwargs}
        contexts = {name: kwargs[name] for name in self.context_names}
        feedback = self.readout.feedback(outputs)
        inputs = (self.fork.apply(feedback, as_dict=True)
                  if self.fork else {'feedback': feedback})

        # Run the recurrent network
        results = self.transition.apply(
            mask=mask, return_initial_states=True, as_dict=True,
            **dict_union(inputs, states, contexts))

        # Separate the deliverables
        states = {name: results[name][:-1] for name in self.state_names}
        glimpses = {name: results[name] for name in self.glimpse_names}

        # Compute the cost
        feedback = tensor.roll(feedback, 1, 0)
        feedback = tensor.set_subtensor(
            feedback[0],
            self.readout.feedback(self.readout.initial_outputs(
                batch_size, **contexts)))
        readouts = self.readout.readout(
            feedback=feedback, **dict_union(states, glimpses, contexts))
        costs = self.readout.cost(readouts, outputs)

        for name, variable in glimpses.items():
            application_call.add_auxiliary_variable(
                variable.copy(), name=name)

        # In case the user needs some glimpses or states or smth else
        return costs

    @recurrent
    def generate(self, outputs, **kwargs):
        """A sequence generation step.

        Parameters
        ----------
        outputs : :class:`~tensor.TensorVariable`
            The outputs from the previous step.

        Notes
        -----
            The contexts, previous states and glimpses are expected
            as keyword arguments.

        """
        states = {name: kwargs[name] for name in self.state_names}
        contexts = {name: kwargs[name] for name in self.context_names}
        glimpses = {name: kwargs[name] for name in self.glimpse_names}

        next_glimpses = self.transition.take_glimpses(
            as_dict=True, **dict_union(states, glimpses, contexts))
        next_readouts = self.readout.readout(
            feedback=self.readout.feedback(outputs),
            **dict_union(states, next_glimpses, contexts))
        next_outputs = self.readout.emit(next_readouts)
        next_costs = self.readout.cost(next_readouts, next_outputs)
        next_feedback = self.readout.feedback(next_outputs)
        next_inputs = (self.fork.apply(next_feedback, as_dict=True)
                       if self.fork else {'feedback': next_feedback})
        next_states = self.transition.compute_states(
            as_list=True,
            **dict_union(next_inputs, states, next_glimpses, contexts))
        return (next_states + [next_outputs] +
                list(next_glimpses.values()) + [next_costs])

    @generate.delegate
    def generate_delegate(self):
        return self.transition.apply

    @generate.property('states')
    def generate_states(self):
        return self.state_names + ['outputs'] + self.glimpse_names

    @generate.property('outputs')
    def generate_outputs(self):
        return (self.state_names + ['outputs'] +
                self.glimpse_names + ['costs'])

    def get_dim(self, name):
        if name in self.state_names + self.context_names + self.glimpse_names:
            return self.transition.get_dim(name)
        elif name == 'outputs':
            return self.readout.get_dim(name)
        return super(BaseSequenceGenerator, self).get_dim(name)

    @application
    def initial_state(self, name, batch_size, *args, **kwargs):
        if name == 'outputs':
            return self.readout.initial_outputs(batch_size)
        elif name in self.state_names + self.glimpse_names:
            return self.transition.initial_state(name, batch_size,
                                                 *args, **kwargs)
        else:
            # TODO: raise a nice exception
            assert False


@add_metaclass(ABCMeta)
class AbstractEmitter(Brick):
    """The interface for the emitter component of a readout."""
    @abstractmethod
    def emit(self, readouts):
        pass

    @abstractmethod
    def cost(self, readouts, outputs):
        pass

    @abstractmethod
    def initial_outputs(self, batch_size, *args, **kwargs):
        pass


@add_metaclass(ABCMeta)
class AbstractFeedback(Brick):
    """The interface for the feedback component of a readout."""
    @abstractmethod
    def feedback(self, outputs):
        pass


@add_metaclass(ABCMeta)
class AbstractReadout(AbstractEmitter, AbstractFeedback):
    """The interface for the readout component of a sequence generator.

    .. todo::

       Explain what the methods should do.

    """
    @abstractmethod
    def readout(self, **kwargs):
        pass


@add_metaclass(ABCMeta)
class Readout(AbstractReadout):
    """Readout brick with separated emitting and feedback parts.

    Parameters
    ----------
    readout_dim : int
        The dimension of the readout.
    emitter : an instance of :class:`AbstractEmitter`
        The emitter component.
    feedbacker : an instance of :class:`AbstractFeedback`
        The feedback component.

    """
    @lazy
    def __init__(self, readout_dim=None, emitter=None, feedbacker=None,
                 **kwargs):
        super(Readout, self).__init__(**kwargs)
        self.readout_dim = readout_dim

        if not emitter:
            emitter = TrivialEmitter(readout_dim)
        if not feedbacker:
            feedbacker = TrivialFeedback(readout_dim)
        self.emitter = emitter
        self.feedbacker = feedbacker

        self.children = [self.emitter, self.feedbacker]

    def _push_allocation_config(self):
        self.emitter.readout_dim = self.get_dim('readouts')
        self.feedbacker.output_dim = self.get_dim('outputs')

    @application
    def emit(self, readouts):
        return self.emitter.emit(readouts)

    @application
    def cost(self, readouts, outputs):
        return self.emitter.cost(readouts, outputs)

    @application
    def initial_outputs(self, batch_size, *args, **kwargs):
        return self.emitter.initial_outputs(batch_size, **kwargs)

    @application(outputs=['feedback'])
    def feedback(self, outputs):
        return self.feedbacker.feedback(outputs)

    def get_dim(self, name):
        if name == 'outputs':
            return self.emitter.get_dim(name)
        elif name == 'feedback':
            return self.feedbacker.get_dim(name)
        elif name == 'readouts':
            return self.readout_dim
        return super(Readout, self).get_dim(name)


class LinearReadout(Readout, Initializable):
    """Readout computed as sum of linear projections.

    Parameters
    ----------
    readout_dim : int
        The dimension of the readout.
    source_names : list of strs
        The names of information sources.

    Notes
    -----
    See :class:`.Initializable` for initialization parameters.

    """
    @lazy
    def __init__(self, readout_dim, source_names, **kwargs):
        super(LinearReadout, self).__init__(readout_dim, **kwargs)
        self.readout_dim = readout_dim
        self.source_names = source_names

        self.projectors = [MLP(name="project_{}".format(name),
                               activations=[Identity()])
                           for name in self.source_names]
        self.children.extend(self.projectors)

    def _push_allocation_config(self):
        super(LinearReadout, self)._push_allocation_config()
        for name, projector in zip(self.source_names, self.projectors):
            projector.dims[0] = self.source_dims[name]
            projector.dims[-1] = self.readout_dim

    @application
    def readout(self, **kwargs):
        projections = [projector.apply(kwargs[name]) for name, projector in
                       zip(self.source_names, self.projectors)]
        if len(projections) == 1:
            return projections[0]
        return sum(projections[1:], projections[0])


class TrivialEmitter(AbstractEmitter):
    """An emitter for the trivial case when readouts are outputs.

    Parameters
    ----------
    readout_dim : int
        The dimension of the readout.

    """
    @lazy
    def __init__(self, readout_dim, **kwargs):
        super(TrivialEmitter, self).__init__(**kwargs)
        self.readout_dim = readout_dim

    @application
    def emit(self, readouts):
        return readouts

    @application
    def initial_outputs(self, batch_size, *args, **kwargs):
        return tensor.zeros((batch_size, self.readout_dim))

    def get_dim(self, name):
        if name == 'outputs':
            return self.readout_dim
        return super(TrivialEmitter, self).get_dim(name)


class SoftmaxEmitter(AbstractEmitter, Initializable, Random):
    """A softmax emitter for the case of integer outputs.

    Interprets readout elements as energies corresponding to their indices.

    """
    def _probs(self, readouts):
        shape = readouts.shape
        return tensor.nnet.softmax(readouts.reshape(
            (tensor.prod(shape[:-1]), shape[-1]))).reshape(shape)

    @application
    def emit(self, readouts):
        probs = self._probs(readouts)
        return self.theano_rng.multinomial(pvals=probs).argmax(axis=-1)

    @application
    def cost(self, readouts, outputs):
        probs = self._probs(readouts)
        max_output = probs.shape[-1]
        flat_outputs = outputs.flatten()
        num_outputs = flat_outputs.shape[0]
        return -tensor.log(
            probs.flatten()[max_output * tensor.arange(num_outputs) +
                            flat_outputs].reshape(outputs.shape))

    @application
    def initial_outputs(self, batch_size, *args, **kwargs):
        return tensor.zeros((batch_size,), dtype='int64')

    def get_dim(self, name):
        if name == 'outputs':
            return 0
        return super(SoftmaxEmitter, self).get_dim(name)


class TrivialFeedback(AbstractFeedback):
    """A feedback brick for the case when readout are outputs."""
    @lazy
    def __init__(self, output_dim, **kwargs):
        super(TrivialFeedback, self).__init__(**kwargs)
        self.output_dim = output_dim

    @application(outputs=['feedback'])
    def feedback(self, outputs):
        return outputs

    def get_dim(self, name):
        if name == 'feedback':
            return self.output_dim
        return super(TrivialFeedback, self).get_dim(name)


class LookupFeedback(AbstractFeedback, Initializable):
    """A feedback brick for the case when readout are integers.

    Stores and retrieves distributed representations of integers.

    Notes
    -----
    Currently works only with lazy initialization (can not be initialized
    with a single constructor call).

    """
    def __init__(self, num_outputs=None, feedback_dim=None, **kwargs):
        super(LookupFeedback, self).__init__(**kwargs)
        self.num_outputs = num_outputs
        self.feedback_dim = feedback_dim

        self.lookup = LookupTable(num_outputs, feedback_dim,
                                  weights_init=self.weights_init)
        self.children = [self.lookup]

    def _push_allocation_config(self):
        self.lookup.length = self.num_outputs
        self.lookup.dim = self.feedback_dim

    @application
    def feedback(self, outputs):
        assert self.output_dim == 0
        return self.lookup.lookup(outputs)

    def get_dim(self, name):
        if name == 'feedback':
            return self.feedback_dim
        return super(LookupFeedback, self).get_dim(name)


class FakeAttentionRecurrent(AbstractAttentionRecurrent, Initializable):
    """Adds fake attention interface to a transition.

    Notes
    -----
    Currently works only with lazy initialization (can not be initialized
    with a single constructor call).

    """
    def __init__(self, transition, **kwargs):
        super(FakeAttentionRecurrent, self).__init__(**kwargs)
        self.transition = transition

        self.state_names = transition.apply.states
        self.context_names = transition.apply.contexts
        self.glimpse_names = []

        self.children = [self.transition]

    @application
    def apply(self, *args, **kwargs):
        return self.transition.apply(*args, **kwargs)

    @apply.delegate
    def apply_delegate(self):
        return self.transition.apply

    @application
    def compute_states(self, *args, **kwargs):
        return self.transition.apply(iterate=False, *args, **kwargs)

    @compute_states.delegate
    def compute_states_delegate(self):
        return self.transition.apply

    @application(outputs=[])
    def take_glimpses(self, *args, **kwargs):
        return None

    @application
    def initial_state(self, state_name, batch_size, *args, **kwargs):
        return self.transition.initial_state(state_name, batch_size,
                                             *args, **kwargs)

    def get_dim(self, name):
        return self.transition.get_dim(name)


class SequenceGenerator(BaseSequenceGenerator):
    """A more user-friendly interface for BaseSequenceGenerator.

    Parameters
    ----------
    readout : instance of :class:`AbstractReadout`
        The readout component for the sequence generator.
    transition : instance of :class:`.BaseRecurrent`
        The recurrent transition to be used in the sequence generator.
        Will be combined with `attention`, if that one is given.
    attention : :class:`.Brick`
        The attention mechanism to be added to ``transition``. Can be
        ``None``, in which case no attention mechanism is used.
    add_contexts : bool
        If ``True``, the :class:`AttentionRecurrent` wrapping the
        `transition` will add additional contexts for the attended and
        its mask.

    Notes
    -----
    Currently works only with lazy initialization (uses blocks that can not
    be constructed with a single call).

    """
    def __init__(self, readout, transition, attention=None,
                 fork_inputs=None, add_contexts=True, **kwargs):
        if not fork_inputs:
            fork_inputs = [name for name in transition.apply.sequences
                           if name != 'mask']

        fork = Fork(fork_inputs)
        if attention:
            distribute = Distribute(fork_inputs,
                                    attention.take_glimpses.outputs[0])
            transition = AttentionRecurrent(
                transition, attention, distribute,
                add_contexts=add_contexts, name="att_trans")
        else:
            transition = FakeAttentionRecurrent(transition,
                                                name="with_fake_attention")
        super(SequenceGenerator, self).__init__(
            readout, transition, fork, **kwargs)
