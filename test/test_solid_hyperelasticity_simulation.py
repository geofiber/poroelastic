from Hyperelasticity_Cube import Hyperelastic_Cube


""" Set up for simulation using the poroelastic package. """
import uuid
import sys
from ufl import grad as ufl_grad
import sys
import numpy as np
import dolfin as df
import poroelastic as poro
from poroelastic.material_models import *
import poroelastic.utils as utils
import pdb


# Compiler parameters
flags = ["-O3", "-ffast-math", "-march=native"]
parameters["form_compiler"]["quadrature_degree"] = 4
parameters["form_compiler"]["representation"] = "uflacs"
parameters["form_compiler"]["cpp_optimize"] = True
parameters["form_compiler"]["cpp_optimize_flags"] = " ".join(flags)
parameters["allow_extrapolation"] = True

set_log_level(40)

class HyperElasticProblem(object):
    """
    Boundary marker labels:
    - inflow (Neumann BC in fluid mass increase)
    - outflow (Neumann BC in fluid mass increase)
    """

    def __init__(self, mesh, params, boundaries=None, markers={},fibers=None, territories=None):
        self.mesh = mesh
        self.params = params
        self.markers = markers
        self.N = int(self.params['Parameter']['N'])

        if boundaries != None:
            self.ds = ds(subdomain_data=boundaries)
        else:
            self.ds = ds()

        if territories == None:
            self.territories = MeshFunction("size_t", mesh, mesh.topology().dim())
            self.territories.set_all(0)
        else:
            self.territories = territories

        # Create function spaces
        self.FS_S, self.FS_M, self.FS_F, self.FS_V = self.create_function_spaces()

        # Create solution functions
        self.Us = Function(self.FS_S)
        self.Us_n = Function(self.FS_S)
        self.mf = Function(self.FS_M)
        self.mf_n = Function(self.FS_M)
        self.Uf = [Function(self.FS_V) for i in range(self.N)]
        if self.N == 1:
            self.p = [Function(self.FS_M)]
        else:
            self.p =\
                [Function(self.FS_M.sub(0).collapse()) for i in range(self.N)]

        rho = self.rho()
        phi0 = self.phi()
        if self.N == 1:
            self.phif = [variable(self.mf/rho + phi0)]
        else:
            self.phif = [variable(self.mf[i]/rho + phi0) for i in range(self.N)]

        self.sbcs = []
        self.fbcs = []
        self.pbcs = []
        self.tconditions = []

        if self.params['Material']["material"] == "isotropic exponential form":
            self.material = IsotropicExponentialFormMaterial(self.params['Material'])
        elif self.params['Material']["material"] == "linear poroelastic":
            self.material = LinearPoroelasticMaterial(self.params['Material'])
        elif self.params['Material']["material"] == "Neo-Hookean":
            self.material = NeoHookeanMaterial(self.params['Material'])


        # Set variational forms
        self.SForm, self.dSForm = self.set_solid_variational_form({})
        self.MForm, self.dMForm = self.set_fluid_variational_form()

    def create_function_spaces(self):
        V1 = VectorElement('P', self.mesh.ufl_cell(), 1)
        V2 = VectorElement('P', self.mesh.ufl_cell(), 2)
        P1 = FiniteElement('P', self.mesh.ufl_cell(), 1)
        P2 = FiniteElement('P', self.mesh.ufl_cell(), 2)
        TH = MixedElement([V2, P1]) # Taylor-Hood element
        FS_S = FunctionSpace(self.mesh, TH)
        if self.N == 1:
            FS_M = FunctionSpace(self.mesh, P1)
        else:
            M = MixedElement([P1 for i in range(self.N)])
            FS_M = FunctionSpace(self.mesh, M)
        FS_F = FunctionSpace(self.mesh, P2)
        FS_V = FunctionSpace(self.mesh, V1)
        return FS_S, FS_M, FS_F, FS_V

    def add_solid_dirichlet_condition(self, condition, *args, **kwargs):
        if 'n' in kwargs.keys():
            n = kwargs['n']
            dkwargs = {}
            if 'method' in kwargs.keys():
                dkwargs['method'] = kwargs['method']
            self.sbcs.append(DirichletBC(self.FS_S.sub(0).sub(n), condition,
                                *args, **dkwargs))

        else:
            self.sbcs.append(DirichletBC(self.FS_S.sub(0), condition,
                                *args, **kwargs))
        if 'time' in kwargs.keys() and kwargs['time']:
            self.tconditions.append(condition)

    def add_solid_neumann_conditions(self, conditions, boundaries):
        self.SForm, self.dSForm =\
                    self.set_solid_variational_form(zip(conditions, boundaries))

    def add_fluid_dirichlet_condition(self, condition, *args, **kwargs):
        if 'source' in kwargs.keys() and kwargs['source']:
            sub = 0 if self.N > 1 else 0
        else:
            sub = self.N-1
        if 'time' in kwargs.keys() and kwargs['time']:
            self.tconditions.append(condition)
        if self.N == 1:
            self.fbcs.append(DirichletBC(self.FS_M, condition, *args))
        else:
            self.fbcs.append(DirichletBC(self.FS_M.sub(sub), condition, *args))


    def add_pressure_dirichlet_condition(self, condition, *args, **kwargs):
        if 'source' in kwargs.keys() and kwargs['source']:
            sub = 0 if self.N > 1 else 0
        else:
            sub = self.N-1
        if 'time' in kwargs.keys() and kwargs['time']:
            self.tconditions.append(condition)
        self.pbcs.append(DirichletBC(self.FS_F, condition, *args))


    def sum_fluid_mass(self):
        if self.N == 1:
            return self.mf/self.params['Parameter']['rho']
        else:
            return sum([self.mf[i]
                    for i in range(self.N)])/self.params['Parameter']['rho']



    def set_solid_variational_form(self, neumann_bcs):

        U = self.Us
        dU, L = split(U)
        V = TestFunction(self.FS_S)
        v, w = split(V)
        #v = V


        # parameters
        rho = self.rho()
        phi0 = Constant(self.params['Parameter']['phi'])
        m = self.sum_fluid_mass()
        # Kinematics
        n = FacetNormal(self.mesh)
        #Return the dimension of the space this cell is embedded in
        d = dU.geometric_dimension()
        self.I = Identity(d)
        self.F = variable(self.I + ufl_grad(dU))
        self.J = variable(det(self.F))
        self.C = variable(self.F.T*self.F)

        self.Psi = self.material.constitutive_law(J=self.J, C=self.C,
                                                phi=phi0)
        Psic = self.Psi*dx +  L*(self.J-Constant(1)-m/rho)*dx

        for condition, boundary in neumann_bcs:
            Psic += dot(condition*n, dU)*self.ds(boundary)

        Form = derivative(Psic, U, V)
        dF = derivative(Form, U, TrialFunction(self.FS_S))

        return Form, dF

    def set_fluid_variational_form(self):

        m = self.mf
        m_n = self.mf_n
        dU, L = self.Us.split(True)
        dU_n, L_n = self.Us_n.split(True)

        # Parameters
        self.qi = self.q_in()
        q_out = self.q_out()
        rho = self.rho()
        beta = self.beta()
        k = Constant(1/self.dt())
        dt = Constant(self.dt())
        th, th_ = self.theta()
        n = FacetNormal(self.mesh)

        # VK = TensorFunctionSpace(self.mesh, "P", 1)
        # if d == 2:
        #     exp = Expression((('0.5', '0.0'),('0.0', '1.0')), degree=1)
        # elif d == 3:
        #     exp = Expression((('1.0', '0.0', '0.0'),('0.0', '1.0', '0.0'),
        #                         ('0.0', '0.0', '1.0')), degree=1)
        # self.K = project(Ki*exp, VK, solver_type='mumps')

        # theta-rule / Crank-Nicolson
        M = th*m + th_*m_n

        # Fluid variational form
        A = variable(rho * self.J * inv(self.F) * self.K() * inv(self.F.T))
        if self.N == 1:
            vm = TestFunction(self.FS_M)
            Form = k*(m - m_n)*vm*dx + dot(grad(M), k*(dU-dU_n))*vm*dx +\
                    inner(-A*grad(self.p[0]), grad(vm))*dx

            # Add inflow terms
            Form += -rho*self.qi*vm*dx

            # Add outflow term
            Form += rho*q_out*vm*dx

        else:
            vm = TestFunctions(self.FS_M)
            Form = sum([k*(m[i] - m_n[i])*vm[i]*dx for i in range(self.N)])\
                + sum([dot(grad(M[i]), k*(dU-dU_n))*vm[i]*dx
                                                    for i in range(self.N)])\
                + sum([inner(-A*grad(self.p[i]), grad(vm[i]))*dx
                                                    for i in range(self.N)])

            # Compartment exchange
            for i in range(len(beta)):
                Form += -self.J*beta[i]*((self.p[i] - self.p[i+1])*vm[i] +\
                                        (self.p[i+1] - self.p[i])*vm[i+1])*dx

            # Add inflow terms
            Form += -rho*self.qi*vm[0]*dx

            # Add outflow term
            Form += rho*q_out*vm[-1]*dx

        dF = derivative(Form, m, TrialFunction(self.FS_M))

        return Form, dF



    def fluid_solid_coupling(self):
        TOL = self.TOL()
        dU, L = self.Us.split(True)
        if self.N == 1:
            FS = self.FS_M
        else:
            FS = self.FS_M.sub(0).collapse()
        for i in range(self.N):
            p = TrialFunction(self.FS_F)
            q = TestFunction(self.FS_F)
            a = p*q*dx
            Ll = (tr(diff(self.Psi, self.F) * self.F.T))/self.phif[i]*q*dx - L*q*dx
            A = assemble(a)
            b = assemble(Ll)
            [bc.apply(A, b) for bc in self.pbcs]
            solver = KrylovSolver('minres', 'hypre_amg')
            prm = solver.parameters
            prm.absolute_tolerance = TOL
            prm.relative_tolerance = TOL*1e3
            prm.maximum_iterations = 1000
            p = Function(self.FS_F)
            solver.solve(A, p.vector(), b)
            self.p[i].assign(project(p, FS))

    def calculate_flow_vector(self):
        FS = VectorFunctionSpace(self.mesh, 'P', 1)
        dU, L = self.Us.split(True)
        m = TrialFunction(self.FS_V)
        mv = TestFunction(self.FS_V)

        # Parameters
        rho = Constant(self.rho())
        #from IPython import embed; embed()
        for i in range(self.N):
            a = (1/rho)*inner(self.F*m, mv)*dx
            L = inner(-self.J*self.K()*inv(self.F.T)*grad(self.p[i]), mv)*dx

            solve(a == L, self.Uf[i], solver_parameters={"linear_solver": "minres",
                                                    "preconditioner": "hypre_amg"})
            # else:
            #     print("\n ****************************************\n\
            #     Flow vector can only be calculated if dt!= tf and qi and qo!\
            #     Simulation will continue without calculating the flow vector.\
            #     \n **************************************** )")
            #     break



    def move_mesh(self):
        dU, L = self.Us.split(True)
        #dU = self.Us
        ALE.move(self.mesh, project(dU, VectorFunctionSpace(self.mesh, 'P', 1)))


    def choose_solver(self, prob):
        if self.params['Simulation']['solver'] == 'direct':
            return self.direct_solver(prob)
        else:
            return self.iterative_solver(prob)

    def solve(self):
        #pdb.set_trace()
        comm = mpi_comm_world()
        mpiRank = MPI.rank(comm)

        tol = self.TOL()
        maxiter = 100
        t = 0.0
        dt = self.dt()

        mprob = NonlinearVariationalProblem(self.MForm, self.mf, bcs=self.fbcs,
                                             J=self.dMForm)
        msol = self.choose_solver(mprob)

        mprob = NonlinearVariationalProblem(self.MForm, self.mf, bcs=self.fbcs,
                                            J=self.dMForm)
        msol = self.choose_solver(mprob)

        sprob = NonlinearVariationalProblem(self.SForm, self.Us, bcs=self.sbcs,J=self.dSForm)
        ssol = self.choose_solver(sprob)

        while t < self.params['Parameter']['tf']:

            if mpiRank == 0:utils.print_time(t)

            iter = 0
            eps = 1
            mf_ = Function(self.FS_F)

            while eps > tol and iter < maxiter:
                mf_.assign(self.p[0])
                ssol.solve()
                #sys.exit()
                self.fluid_solid_coupling()
                msol.solve()
                e = self.p[0] - mf_
                eps = np.sqrt(assemble(e**2*dx))
                iter += 1



            # Store current solution as previous
            self.Us_n.assign(self.Us)
            self.mf_n.assign(self.mf)

            # Calculate fluid vector
            if self.params['Parameter']['dt'] == self.params['Parameter']['tf']:
                print("\n ****************************************\n\
                Flow vector can only be calculated if dt!= tf \n\
                Simulation will continue without calculating the flow vector.\
                \n **************************************** )")

            elif (self.params['Parameter']['qo'] == 0.0) and (self.params['Parameter']['qi'] == 0.0):
                print("\n ****************************************\n\
                Flow vector can only be calculated if qi,qo!=0 ! \n\
                Simulation will continue without calculating the flow vector.\
                \n **************************************** )")

            else:
                self.calculate_flow_vector()


            # transform mf into list
            if self.N > 1:
                mf_list = [self.mf.sub(i) for i in range(self.N)]

            if self.N ==1:
                yield self.mf, self.Uf, self.p, self.Us, t
            else:
                yield mf_list, self.Uf, self.p, self.Us, t

            self.move_mesh()

            t += dt

        # Add a last print so that next output won't overwrite my time print statements
        print()


    def direct_solver(self, prob):
        sol = NonlinearVariationalSolver(prob)
        sol.parameters['newton_solver']['linear_solver'] = 'mumps'
        sol.parameters['newton_solver']['lu_solver']['reuse_factorization'] = True
        sol.parameters['newton_solver']['maximum_iterations'] = 1000
        return sol

    def iterative_solver(self, prob):
        TOL = self.TOL()
        sol = NonlinearVariationalSolver(prob)
        sol.parameters['newton_solver']['linear_solver'] = 'minres'
        sol.parameters['newton_solver']['preconditioner'] = 'hypre_amg'
        sol.parameters['newton_solver']['absolute_tolerance'] = TOL
        sol.parameters['newton_solver']['relative_tolerance'] = TOL*1e3
        sol.parameters['newton_solver']['maximum_iterations'] = 1000
        return sol

    def rho(self):
        return Constant(self.params['Parameter']['rho'])

    def phi(self):
        return Constant(self.params['Parameter']['phi'])

    def beta(self):
        if self.N == 1:
            beta = 0.0
        else:
            beta = self.params['Parameter']['beta']
        if isinstance(beta, float):
            beta = [beta]
        return [Constant(b) for b in beta]


    def dt(self):
        return self.params['Parameter']['dt']

    def theta(self):
        theta = self.params['Parameter']['theta']
        return Constant(theta), Constant(1-theta)

    def q_out(self):
        if isinstance(self.params['Parameter']['qo'], str):
            return Expression(self.params['Parameter']['qo'], degree=1)
        else:
            return Constant(self.params['Parameter']['qo'])

    def q_in(self):
        if isinstance(self.params['Parameter']['qi'], str):
            return Expression(self.params['Parameter']['qi'], degree=1)
        else:
            return Constant(self.params['Parameter']['qi'])

    def K(self):
        # if self.N == 1:
        d = self.mf.geometric_dimension()
        I = Identity(d)
        K = Constant(self.params['Parameter']['K'])
        # if self.fibers:
        #     return K*I
        # else:
        #     return K*I
        return K*I

    def TOL(self):
        return self.params['Parameter']['TOL']

comm = df.mpi_comm_world()
#
#
# Create mesh
#
mesh = df.UnitCubeMesh(16,12,12)
#
#
params = poro.ParamParser(cfgfile='test_solid_hyperelasticity_simulation.cfg')
#
#
data_dir = str(uuid.uuid4())
#
# Add result to parameter dictionary providing section, key, value
#
params.add_data("Simulation", "dir", data_dir)
#
# Print unique simulation ID to screen
#
print("Simulation ID {}".format(data_dir))
#
#
hprob = HyperElasticProblem(mesh, params.p)
#
#
class Left(df.SubDomain):
    def inside(self, x, on_boundary):
        return df.near(x[0], 0.0) and on_boundary

class Right(df.SubDomain):
    def inside(self, x, on_boundary):
        return df.near(x[0], 1.0) and on_boundary

class Top(df.SubDomain):
    def inside(self, x, on_boundary):
        return df.near(x[1], 1.0) and on_boundary

class Bottom(df.SubDomain):
    def inside(self, x, on_boundary):
        return df.near(x[1], 0.0) and on_boundary
#
# Initialize mesh function for boundary domains.
boundaries = df.MeshFunction("size_t", mesh, mesh.topology().dim()-1)
#
boundaries.set_all(0)
#
# Initialize boundary subdomain instances.
#
left = Left()
right = Right()
top = Top()
bottom = Bottom()
#
# Mark boundary subdomains.
#
left.mark(boundaries, 1)
right.mark(boundaries, 2)
top.mark(boundaries, 3)
bottom.mark(boundaries, 4)
#
# Define Dirichlet boundary (x = 0 or x = 1)
# The Dirichlet boundary values are defined using compiled expressions::
#
zero = df.Constant((0,0,0))
r = df.Expression(("scale*0.0",
                "scale*(y0 + (x[1] - y0)*cos(theta) - (x[2] - z0)*sin(theta) - x[1])",
                "scale*(z0 + (x[1] - y0)*sin(theta) + (x[2] - z0)*cos(theta) - x[2])"),
                scale = 0.5, y0 = 0.5, z0 = 0.5, theta = np.pi/7, degree=2)
#
# Define Dirichlet boundary conditions on boundary subdomains for solid
#
hprob.add_solid_dirichlet_condition(zero, boundaries, 1)
hprob.add_solid_dirichlet_condition(r, boundaries, 2)
#

def set_xdmf_parameters(f):
    f.parameters['flush_output'] = True
    f.parameters['functions_share_mesh'] = True
    f.parameters['rewrite_function_mesh'] = False

#
# Setting the number of compartments by the integer found for the key 'Parameter' ,
# 'N' in the input file.cfg.
#
N = int(params.p['Parameter']['N'])

f1 = [df.XDMFFile(comm, '../data/{}/uf{}.xdmf'.format(data_dir, i)) for i in range(N)]
f2 = df.XDMFFile(comm, '../data/{}/mf.xdmf'.format(data_dir))
f3 = [df.XDMFFile(comm, '../data/{}/p{}.xdmf'.format(data_dir, i)) for i in range(N)]
f4 = df.XDMFFile(comm, '../data/{}/du.xdmf'.format(data_dir))
#
# Initialize 'set_xdmf_parameters' for XDMFFiles to be created
#
[set_xdmf_parameters(f1[i]) for i in range(N)]
set_xdmf_parameters(f2)
[set_xdmf_parameters(f3[i]) for i in range(N)]
set_xdmf_parameters(f4)
#
#
# Define new measures associated with exterior boundaries.
dx = df.Measure("dx")
ds = df.Measure("ds")(subdomain_data=boundaries)
#

# Set start variables for the calculations
sum_fluid_mass = 0
theor_fluid_mass = 0
sum_disp = 0
domain_area = 1.0
#
#
phi = params.p['Parameter']["phi"]
dt = params.p['Parameter']["dt"]
tf = params.p['Parameter']["tf"]
rho = params.p['Parameter']["rho"]
qi = params.p['Parameter']["qi"]
#
#u = Hyperelastic_Cube(16,12,12)
#from IPython import embed; embed()

for Mf, Uf, p, Us, t in hprob.solve():
    #from IPython import embed; embed()
    dU, L = Us.split(True)
    #dU = Us

    [poro.write_file(f1[i], Uf[i], 'uf{}'.format(i), t) for i in range(N)]
    poro.write_file(f2, Mf, 'mf', t)
    [poro.write_file(f3[i], p[i], 'p{}'.format(i), t) for i in range(N)]
    poro.write_file(f4, dU, 'du', t)
    #diff = project(dU-u, dU.function_space())

    domain_area += df.assemble(df.div(dU)*dx)*(1-phi)



    #sum_fluid_mass += df.assemble(Mf*dx)
    theor_fluid_mass += qi*rho*dt
    #theor_sol = theor_fluid_mass*domain_area
    sum_disp += df.assemble(dU[0]*ds(4))
    #avg_error.append(np.sqrt(((df.assemble(Mf*dx)-theor_sol)/theor_sol)**2))
    #print(theor_sol, df.assemble(Mf*dx))

[f1[i].close() for i in range(N)]
f2.close()
[f3[i].close() for i in range(N)]
f4.close()
#
# error = sum(avg_error)/len(avg_error)
#
#
params.write_config('../data/{}/{}.cfg'.format(data_dir, data_dir))

print("I finished the HyperElasticProblem Part!")
#


u = Hyperelastic_Cube(df.UnitCubeMesh(16,12,12))
error = errornorm(u, dU, 'L2')
print("The error is: {}".format(error))

def test_error(error):
    assert error < 0.1
