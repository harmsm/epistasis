import inspect
import numpy as np
import json
from functools import wraps
from scipy.optimize import curve_fit

from sklearn.base import BaseEstimator, RegressorMixin
from ..base import BaseModel, X_fitter, X_predictor

from ..linear.regression import EpistasisLinearRegression
from ..linear.classifiers import ModelPreprocessor

from epistasis.stats import pearson
# decorators for catching errors
from gpmap.utils import ipywidgets_missing

# Try to import ipython specific tools
try:
    import ipywidgets
except ImportError:
    pass

class Parameters(object):
    """ A container object for parameters extracted from a nonlinear fit.
    """
    def __init__(self, params):
        self._param_list = params
        self.n = len(self._param_list)
        self._mapping, self._mapping_  = {}, {}
        for i in range(self.n):
            setattr(self, self._param_list[i], 0)
            self._mapping_[i] = self._param_list[i]
            self._mapping[self._param_list[i]] = i

    def to_json(self, filename):
        """Write parameters to json
        """
        with open(filename, "w") as f:
            json.dump(f, self())

    def __call__(self):
        """Return parameters if the instance is called."""
        return dict(zip(self.keys, self.values))

    @property
    def keys(self):
        """Get ordered list of params"""
        return self._param_list

    @property
    def values(self):
        """Get ordered list of params"""
        vals = []
        for p in self._param_list:
            vals.append(getattr(self, p))
        return vals

    def _set_param(self, param, value):
        """ Set Parameter value. Method is not exposed to user.
        param can be either the name of the parameter or its index in this object.
        """
        # If param is an index, get name from mappings
        if type(param) == int or type(param) == float:
            param = self._mapping_[param]
        setattr(self, param, value)

    def get_params(self):
        """ Get an ordered list of the parameters."""
        return [getattr(self, self._mapping_[i]) for i in range(len(self._mapping_))]

class EpistasisNonlinearRegression(RegressorMixin, BaseEstimator, BaseModel, ModelPreprocessor):
    """Epistasis estimator for nonlinear genotype-phenotype maps.

    Parameters
    ----------
    """
    def __init__(self,
        function,
        reverse,
        order=1,
        model_type="global",
        fix_linear=True,
        **kwargs):

        # Do some inspection to
        # Get the parameters from the nonlinear function argument list
        function_sign = inspect.signature(function)
        parameters = list(function_sign.parameters.keys())
        if parameters[0] != "x":
            raise Exception(""" First argument of the nonlinear function must be `x`. """)

        # Set up the function for fitting.
        self.function = function
        self.reverse = reverse

        # Construct parameters object
        #self.__parameters =
        self.parameters = Parameters(parameters[1:])
        self.set_params(order=order,
            model_type=model_type,
            fix_linear=fix_linear)

    @X_fitter
    def fit(self, X=None, y=None, sample_weight=None, use_widgets=False, **parameters):
        """Fit nonlinearity in genotype-phenotype map.

        Parameters
        ----------
        X : 2-d array
            independent data; samples.
        y : array
            dependent data; observations.
        sample_weight : array (default: None, assumed even weight)
            weights for fit.

        Notes
        -----
        Also, will create IPython widgets to sweep through initial parameters guesses.

        This works by, first, fitting the coefficients using a linear epistasis model as initial
        guesses (along with user defined kwargs) for the nonlinear model.

        kwargs should be ranges of guess values for each parameter. They are are turned into
        slider widgets for varying these guesses easily. The kwarg needs to match the name of
        the parameter in the nonlinear fit.
        """
        if hasattr(self, 'gpm') is False:
            raise Exception("This model will not work if a genotype-phenotype "
                "map is not attached to the model class. Use the `attach_gpm` method")

        # ----------------------------------------------------------------------
        # Part 0: Prepare model for fitting and run fit
        # ----------------------------------------------------------------------

        # Fit with an additive model
        self.Additive = EpistasisLinearRegression(order=1, model_type=self.model_type)
        self.Additive.attach_gpm(self.gpm)
        #if self.Additive

        # Prepare a high-order model
        self.Linear = EpistasisLinearRegression(order=self.order, model_type=self.model_type)
        self.Linear.X = X
        self.coef_ = np.zeros(len(self.epistasis.sites))

        ## Use widgets to guess the value?
        if use_widgets:
            # Build fitting method to pass into widget box
            def fitting(**parameters):
                """ Callable to be controlled by widgets. """
                # Fit the nonlinear least squares fit
                self._fit_(X, y, sample_weight=sample_weight, **parameters)
                #if print_stats:
                # Print score
                print("R-squared of fit: " + str(self.score()))
                # Print parameters
                for kw in self.parameters._mapping:
                    print(kw + ": " + str(getattr(self.parameters, kw)))
            # Construct and return the widget box
            widgetbox = ipywidgets.interactive(fitting, **parameters)
            return widgetbox
        # Don't use widgets to fit data
        else:
            self._fit_(X, y, sample_weight=sample_weight, **parameters)
        return self

    def _fit_(self, X=None, y=None, sample_weight=None, **kwargs):
        """Estimate the scale of multiple mutations in a genotype-phenotype map."""
        # ----------------------------------------------------------------------
        # Part 1: Estimate average, independent mutational effects and fit
        #         nonlinear scale.
        # ----------------------------------------------------------------------
        self.Additive.fit()
        x = self.Additive.predict()

        # Set up guesses
        guesses = np.ones(self.parameters.n)
        for kw in kwargs:
            index = self.parameters._mapping[kw]
            guesses[index] = kwargs[kw]

        # Convert weights to variances on fit parameters.
        if sample_weight is None:
            sigma = None
        else:
            sigma = 1 / np.sqrt(sample_weight)

        # Fit with curve_fit, using
        popt, pcov = curve_fit(self.function, x, y, p0=guesses, sigma=sigma, method="trf")
        for i in range(0, self.parameters.n):
            self.parameters._set_param(i, popt[i])

        # ----------------------------------------------------------------------
        # Part 3: Fit high-order, linear model.
        # ----------------------------------------------------------------------

        # Construct a linear epistasis model.
        if self.order > 1:
            linearized_y = self.reverse(y, *self.parameters.values)
            # Now fit with a linear epistasis model.
            self.Linear.fit(X=self.Linear.X, y=linearized_y)
        else:
            self.Linear = self.Additive
        self.coef_ = self.Linear.coef_

    @X_predictor
    def predict(self, X=None):
        """Predict new targets from model."""
        x = self.Linear.predict(X)
        y = self.function(x, *self.parameters.values)
        return y

    @X_fitter
    def score(self, X=None, y=None):
        """Calculates the squared-pearson coefficient for the nonlinear fit.
        """
        y_pred = self.function(self.Additive.predict(X=self.Additive.X), *self.parameters.get_params())
        y_rev = self.reverse(y, *self.parameters.get_params())
        return pearson(y, y_pred)**2, self.Linear.score(X=self.Linear.X, y=y_rev)

    @property
    def thetas(self):
        """Get all parameters in the model as a single array. This concatenates
        the nonlinear parameters and high-order epistatic coefficients.
        """
        return np.concatenate((self.parameters.values, self.Linear.coef_))

    @X_predictor
    def hypothesis(self, X=None, thetas=None):
        """Given a set of parameters, compute a set of phenotypes. This is method
        can be used to test a set of parameters (Useful for bayesian sampling).
        """
        # Test that a maximum likelihood model has been
        # NEED TO WRITE THIS CHECK.
        if hasattr(self, "X") is False:
            raise Exception("A model matrix X needs to be attached to the model. "
                "Try calling `X_constructor()`.")

        # ----------------------------------------------------------------------
        # Part 0: Break up thetas
        # ----------------------------------------------------------------------
        i, j = self.parameters.n, self.epistasis.n
        parameters = thetas[:i]
        epistasis = thetas[i:i+j]
        # Part 1: Linear portion
        y1 = np.dot(X, epistasis)
        # Part 2: Nonlinear portion
        y2 = self.function(y1, *parameters)
        return y2
