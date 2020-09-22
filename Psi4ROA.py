import shelve
import collections
import os
import threading
import subprocess

import roa
import optking
import numpy as np
import psi4
from psi4 import core, p4util
from psi4.core import print_out

from collections.abc import Iterable
def iterable(obj):
    return isinstance(obj, Iterable)

#sadlej-lpol-dl.gbs:
#sadlej-lpol-ds.gbs:
#sadlej-lpol-fl.gbs:
#sadlej-lpol-fs.gbs:
#-  0.9426400000D-01  1.00000000D+01
#+  0.9426400000D-01  0.10000000D+01

class ROA(object):

    def __init__(self, mol, pr=print):  # init with psi4 molecule
        self.mol = mol
        self._analysis_geom = None
        self.omega = None
        self.pr = pr

        # boolean. If true, using atomic displacments; if false, using or normal modes
        self.coord_using_atomic_Cartesians = None
        self.coord_lbls = None # label for displacement coordinates (e.g. 5 or 2_x)
        self.coord_xyzs = None # Cartesian vectors for displacement coordinates

        self.vib_freqs = None #freq, if coordinate is normal mode
        self.vib_modes = None #number of modes if dips are normal mode
    
    def optimize(self, wfn):
        json_output = optking.optimize_psi4(wfn)
        self.E = json_output['energies'][-1]
        self.NRE = json_output['trajectory'][-1]['properties']['nuclear_repulsion_energy']
        self._analysis_geom = np.array(json_output['final_molecule']['geometry'])
        return

    @property
    def analysis_geom(self):
        return self._analysis_geom.copy()

    @property
    def analysis_geom_2D(self):
        return self.analysis_geom.reshape(self.mol.natom(),3).copy()

    def make_displacement_vectors(self, vib_modes=None, geom=None, hessian=None, masses=None):
        """ vib_modes: (list of ints) indicating number of vibrational mode
            along which to displace.  1 means highest frequency mode."""

        Natom = self.mol.natom()
        # if vib_modes is not provided, use 3N cartesian disps
        if vib_modes is None:
            self.pr('Vib. mode numbers omitted, so using simple Cartesian displacements.')
            self.coord_using_atomic_Cartesians = True
            self.coord_xyzs = np.identity(3*Natom)

            self.coord_lbls = []
            for atom in range(1, Natom+1):
                for xyz in ['x','y','z']:
                    self.coord_lbls.append('{}_{}'.format(atom, xyz))

        else:  # just label 1,2,3...
            self.coord_lbls = [i for i in range(1,len(vib_modes)+1)]

            self.pr('Doing displacements for the following normal modes:')
            self.pr('  ', vib_modes)
            self.coord_using_atomic_Cartesians = False
            modes = [m-1 for m in vib_modes]  # internally -1
    
            if geom is None:
                geom   = self.analysis_geom_2D
            if masses is None:
                masses = np.array( [self.mol.mass(i) for i in range(Natom)] )
            if hessian is None:
                hessian = roa.psi4_read_hessian(Natom)
    
            # Do normal mode analysis and return the normal mode vectors (non-MW?) for
            # indices numbering from 0 (highest nu) downward. Modes returned as rows
            (v, nu) = roa.modeVectors(geom,
              masses, hessian, modes, 2, pr)
    
            self.coord_xyzs = v
            self.vib_freqs = nu
            self.vib_modes = vib_modes
    
        return
    

    # Generates disp labels and geometries.
    def make_displaced_geoms(self, disp_size=0.005):
        self.pr('Displacing geometries + and - for 3-point formula')
        self.db['disp_size'] = disp_size
        Natom = self.mol.natom()

        # Construct displaced geometries
        disp_xyzs = []
        for v in self.coord_xyzs:
            dispM = self.analysis_geom
            dispP = self.analysis_geom
            for atom in range(Natom):
                for xyz in range(3):
                    dispM[3*atom+xyz] -= disp_size * v[3*atom+xyz]
                    dispP[3*atom+xyz] += disp_size * v[3*atom+xyz]
            disp_xyzs.append(dispM)
            disp_xyzs.append(dispP)

        disp_lbls = []
        for l in self.coord_lbls:
            for step in ['m','p']:
                disp_lbls.append('{}_{}'.format(l, step))

        for l,g in zip(disp_lbls,disp_xyzs):
            self.db['jobs'].update({l:{'status': 'not_started', 'geom': g}})

        return

    def show_fd_db(self, print_jobs=False):
        self.pr('Database contents:')
        if 'jobs' in self.db:
            self.pr('{} jobs in db'.format(len(self.db['jobs'])))
            self.pr('Jobs:', [k for k in self.db['jobs'].keys()])
            self.pr('Showing first job: ')
            k,v = list(self.db['jobs'].items())[0]
            self.pr('{} : {}'.format(k, v))
        for key, val in self.db.items():
            if key != 'jobs':
                self.pr(' {:25s}{:>10s}'.format(key,str(val)))
        self.pr(' {:25s}{:10d}'.format('Total FD calculations',self.total_fd_db))
        self.pr(' {:25s}{:10d}'.format('Completed FD calculations',self.completed_fd_db))
        self.pr(' {:25s}{:10d}'.format('Remaining FD calculations',self.remaining_fd_db))
        self.pr(' {:25s}{:>10s}'.format('Done with FD calculations',str(self.alldone_fd_db)))
        return

    @property
    def total_fd_db(self): 
        return len(self.db['jobs'])

    @property
    def completed_fd_db(self): 
        return sum(1 for k,v in self.db['jobs'].items() if v['status'] == 'completed')

    @property
    def remaining_fd_db(self): 
        return self.total_fd_db - self.completed_fd_db

    @property
    def alldone_fd_db(self): 
        return True if self.remaining_fd_db == 0 else False

    def init_fd_db(self, wfn, prop, properties_array, omega=None, additional_kwargs=None):
        """
        wfn:  (string) name as passed to calling driver
        prop: (string) the property being computed
        prop_array: (list of strings) properties to go in
            properties kwarg of the properties() cmd in each sub-dir
        additional_kwargs: (list of strings) *optional*
            any additional kwargs that should go in the call to the
            properties() driver method in each subdir
        Returns: nothing
        """

        self.db = shelve.open('fd-database', writeback=True)

        # Create db backup if complete already.
        if '{}_computed'.format(prop) in self.db:
            if self.db['roa_computed']: # if True, old results are in here.
                db2 = shelve.open('fd-database.bak', writeback=True)
                for key,value in self.db.items():
                    db2[key]=value
                db2.close()
                self.db.clear()
            else:
                pass # should we clear it?

        # Initialize clean db
        self.db['{}_computed'.format(prop)] = False
        self.db['inputs_generated'] = False

        # Make sure only one wavelength is given.  Catch now.
        cnt = iterable(omega)
        if omega == None:
            om = 0.0
        elif not iterable(omega):
            om = roa.omega_in_au(omega, 'au')
        elif len(omega) == 1:
            om = roa.omega_in_au(omega[0], 'au')
        elif len(omega) == 2:
            om = roa.omega_in_au(omega[0], omega[1])
        else:
            raise Exception('ROA scattering can only be performed for one wavelength.')
        self.db['omega'] = om
    
        # construct property command
        prop_cmd ="properties('{0}',".format(wfn)
        prop_cmd += "properties=[ '{}' ".format(properties_array[0])
        if len(properties_array) > 1:
            for element in properties_array[1:]:
                prop_cmd += ",'{}'".format(element)
        prop_cmd += "]"
        if additional_kwargs is not None:
            for arg in additional_kwargs:
                prop_cmd += ", {}".format(arg)
        prop_cmd += ")"
        self.db['prop_cmd'] = prop_cmd
    
        # make a dictionary for job status
        self.db['jobs'] = collections.OrderedDict()
    
        return

    def generate_inputs(self, wfn, overwrite=False):
        """ Generates the input files in each sub-directory of the
        distributed finite differences property calculation.
        wfn: ( string ) method name passed to calling driver,
        overwrite: (boolean) whether to overwrite input file if already there
        if True, then output.dat file in same directory is removed too.
        Returns: nothing
        On exit, db['inputs_generated'] has been set True
        """
        cwd = os.getcwd() + '/'
        Natom = self.mol.natom()
    
        for job_lbl,job_info in self.db['jobs'].items():
            dirname = cwd + job_lbl

            if not os.path.exists(dirname): # directory missing
                os.makedirs(dirname)
            elif os.path.isfile(dirname+'/input.dat'): #input file there
                if overwrite:
                    os.remove(dirname+'/input.dat')
                    if os.path.isfile(dirname+'/output.dat'):
                        os.remove(dirname+'/output.dat')
                else: # preserve input file, output if present.
                    return
  
            # Setup up input file string
            inp_template = 'molecule {molname}_{job}'
            inp_template += ' {{\n{molecule_info}\n}}\n{options}\n{jobspec}\n'
            xyz = job_info['geom'].reshape(Natom,3)
            self.mol.set_geometry(core.Matrix.from_array(xyz))
            self.mol.fix_orientation(True)
            self.mol.fix_com(True)
            inputfile = open('{0}/input.dat'.format(job_lbl), 'w')
            inputfile.write("# This input file is autogenerated for finite differences.\n\n")
            inputfile.write(
                inp_template.format(
                    molname=self.mol.name(),
                    job=job_lbl,
                    molecule_info=self.mol.create_psi4_string_from_molecule(),
                    options=p4util.format_options_for_input(),
                    jobspec=self.db['prop_cmd']))
            inputfile.close()
        self.db['inputs_generated'] = True
    
    def close_fd_db(self):
        self.db.close()
        return

    def update_status_fd_db(self, pr=False):
        """ Checks sub_directories, updates db['job'] status 
            Return if completed. """
        cwd = os.getcwd() + '/'

        for job_lbl,job_info in self.db['jobs'].items():
            jobdir = cwd + '/' + job_lbl
            if job_info['status'] in ('not_started', 'running'):
                try:
                    with open("{0}/output.dat".format(jobdir),'r') as outfile:
                        for line in outfile:
                            if 'Psi4 exiting successfully' in line:
                                job_info['status'] = 'completed'
                                break
                            else:
                                job_info['status'] = 'running'
                except:
                    pass
        if pr:
            for job_lbl,job_info in self.db['jobs'].items():
                self.pr('{:15}{:15}'.format(job_lbl,job_info['status']))

        return self.alldone_fd_db

    def run_fd(self, executable, nThreads=1):
        cwd = os.getcwd() + '/'

        # Want to change to json later.
        def runDisp(subDir):
            rootDir = os.getcwd() + '/'
            print("Running displacement %s" % subDir)
            rc = subprocess.call(executable,cwd=rootDir+'/'+subDir)
            if (rc != 0): raise("Tensor calculation failed.")

        def batch(batch_list, batch_size):
            b = []
            for elem in batch_list:
                b.append(elem)
                if len(b) == batch_size:
                    yield b
                    b.clear()
            yield b

        todo = [lbl for lbl,info in self.db['jobs'].items() if info['status'] == 'not_started']

        fd_threads = []
        for b in batch(todo, nThreads):
            fd_threads.clear()
            for job_lbl in b:
                t = threading.Thread(target=runDisp, args = (job_lbl,))
                fd_threads.append(t)
                t.start()
            for t in fd_threads:
                t.join()
        core.clean()

    def analyze_ROA(self, name='CC', gauge='LENGTH', geom=None, masses=None, print_lvl=2):
        """
          name is just a label for the dictionary output, could be driver/wfn
          gauge one or more gauges to analyze
          geometry; only used here if normal mode analysis not get done.
        """
        self.update_status_fd_db()
        if not self.alldone_fd_db:
            self.pr('Finite difference computations not all complete.')
            self.update_status_fd_db(pr=True)
            return

        Natom = self.mol.natom()
        fd_pol = roa.psi4_read_polarizabilities(
                     self.db['jobs'].keys(), self.db['omega'])
        fd_pol = np.array( fd_pol )
        if print_lvl > 2:
            self.pr("Electric-Dipole/Dipole Polarizabilities")
            self.pr(str(fd_pol))

        fd_quad_list = roa.psi4_read_dipole_quadrupole_polarizability(
                           self.db['jobs'].keys(), self.db['omega'])
        fd_quad = []
        for row in fd_quad_list:
            fd_quad.append( np.array(row).reshape(9,3))
        if print_lvl > 2:
            self.pr("Electric-Dipole/Quadrupole Polarizabilities")
            self.pr(str(fd_quad)+'\n')

        # required for IR intensities; could be omitted if absent
        dipder  = roa.psi4_read_dipole_derivatives(Natom)
        if print_lvl > 2:
            self.pr("Dipole Moment Derivatives")
            self.pr(str(dipder)+'\n')

        gauge_todo_options = {
            'LENGTH': ['Length'],
            'VELOCITY': ['Modified Velocity'],
            'BOTH': ['Length', 'Modified Velocity']
        }

        for g in gauge_todo_options[gauge]:
            self.pr('Doing analysis (scatter function) for %s' % g)

            fd_rot = roa.psi4_read_optical_rotation_tensor(
                         self.db['jobs'].keys(), self.db['omega'], g)
            fd_rot = np.array( fd_rot )
            if print_lvl > 2:
                self.pr("Optical Rotation Tensor")
                self.pr(str(fd_rot)+'\n')

            self.pr('\n\n----------------------------------------------------------------------\n')
            self.pr('\t%%%%%%%%%% {} Results %%%%%%%%%%\n'.format(g))
            self.pr('----------------------------------------------------------------------\n\n')

            # Create a name for an output spectral dictionary file
            bas = core.get_global_option('BASIS')
            # Like to put number of basis functions in the output too.
            NBF = core.BasisSet.build(self.mol, 'BASIS', bas).nbf()
            lbl = (name + '/' + bas).upper()
            sp_outfile = core.get_output_file()
            if sp_outfile[-4:] == '.out':
                sp_outfile = sp_outfile[:-4]
            sp_outfile = sp_outfile + '.sp.out'
            if geom == None:
                geom = self.analysis_geom_2D

            if self.coord_using_atomic_Cartesians:
                # If we displaced along 3N Cartesians, then we don't need
                # hessian until here.
                hessian = roa.psi4_read_hessian(Natom)
                if masses == None:
                    masses = np.array( [self.mol.mass(i) for i in range(Natom)] )
                roa.scatter(geom, masses, hessian, dipder, self.db['omega'], self.db['disp_size'],
                  fd_pol, fd_rot, fd_quad, print_lvl=2, ROAdictName=sp_outfile, pr=self.pr,
                  calc_type=lbl, nbf=NBF)
            else:
                roa.modeScatter(self.vib_modes, self.coord_xyzs, self.vib_freqs,
                    geom, masses, dipder, self.db['omega'], self.db['disp_size'], fd_pol, fd_rot, fd_quad,
                    print_lvl=2, ROAdictName=sp_outfile, pr=self.pr, calc_type=lbl, nbf=NBF)

        self.db['roa_computed'] = True


    def compute_dipole_derivatives(self, wfn, prog='psi4', geom=None, disp_points=3, disp_size=0.001):
        if prog == 'psi4':
            # Lacking analytic derivatives, we will do fd of applied electric fields.  We
            # alternately apply + and - electric fields in the x, y, and z directions, and
            # then take finite differences to get the dipole moment derivatives.
            print("Computing dipole moment derivatives with Psi4 by f.d...")

            #Prepare geometry
            Natom = self.mol.natom()
            if geom == None:
                geom = self.analysis_geom_2D
            self.mol.set_geometry(core.Matrix.from_array(geom))
            self.mol.fix_com(True)
            self.mol.fix_orientation(True)
            self.mol.reset_point_group('c1')
            N = self.mol.natom()
        
            if disp_points == 3:
               lambdas = [-1.0*disp_size, 1.0*disp_size]
            elif disp_points == 5:
               lambdas = [-2.0*disp_size, -1.0*disp_size, 1.0*disp_size, +2.0*disp_size]
                
            DmuxDx = psi4.core.Matrix("Dipole derivatives mu_x",N,3)
            DmuyDx = psi4.core.Matrix("Dipole derivatives mu_y",N,3)
            DmuzDx = psi4.core.Matrix("Dipole derivatives mu_z",N,3)
        
            for dipole_xyz, dipole_vector in enumerate([ [1,0,0],[0,1,0],[0,0,1] ]):
               dx = []
               for l in lambdas:
                  core.set_global_option('perturb_h', True)
                  core.set_global_option('perturb_with', 'dipole')
                  scaled_dipole_vector = []
                  for x in dipole_vector:
                      scaled_dipole_vector.append(x*l)
                  core.set_global_option('perturb_dipole', scaled_dipole_vector)
                  dx.append(psi4.gradient(wfn))
        
               for A in range(N):
                  for xyz in range(3):
                     if disp_points == 3:
                        val = (dx[1].get(A,xyz) - dx[0].get(A,xyz)) / (2.0*disp_size)
                     elif disp_points == 5:
                        val = (dx[0].get(A,xyz) - 8.0*dx[1].get(A,xyz) \
                           + 8.0*dx[2].get(A,xyz) - dx[3].get(A,xyz)) / (12.0*disp_size)
        
                     if dipole_xyz == 0:
                        DmuxDx.set(A,xyz, val)
                     elif dipole_xyz == 1:
                        DmuyDx.set(A,xyz, val)
                     elif dipole_xyz == 2:
                        DmuzDx.set(A,xyz, val)
        
            core.set_global_option('PERTURB_H', 0)
            core.set_global_option('PERTURB_DIPOLE', '')
            # write out a file17 with the dipole moment derivatives
            f = open('file17.dat', 'w')
            for i in range(N):
                f.write('{0:20.10f}{1:20.10f}{2:20.10f}\n'.format(DmuxDx.get(i,0), DmuxDx.get(i,1), DmuxDx.get(i,2)))
            for i in range(N):
                f.write('{0:20.10f}{1:20.10f}{2:20.10f}\n'.format(DmuyDx.get(i,0), DmuyDx.get(i,1), DmuyDx.get(i,2)))
            for i in range(N):
                f.write('{0:20.10f}{1:20.10f}{2:20.10f}\n'.format(DmuzDx.get(i,0), DmuzDx.get(i,1), DmuzDx.get(i,2)))
            f.close()
        else:
            raise Exception('Other muder prog not yet implemented')
        return

    def compute_hessian(self, wfn, prog='psi4', geom=None, disp_points=3, disp_size=0.005):
        if prog == 'psi4':
            print("Computing hessian with Psi4...")

            #Prepare geometry
            Natom = self.mol.natom()
            if geom == None:
                geom = self.analysis_geom_2D
            self.mol.set_geometry(core.Matrix.from_array(geom))
            self.mol.fix_com(True)
            self.mol.fix_orientation(True)
            self.mol.reset_point_group('c1')
            Natom = self.mol.natom()

            # core.set_global_option('hessian_write', True)
            # compute the hessian, put in numpy format, then write out file15.dat file.
            psi4_hess = psi4.hessian(wfn, molecule=self.mol)
            npHess = psi4_hess.clone().np
            npHess = np.reshape(npHess, (3*Natom*Natom,3))
            f = open('file15.dat', 'w')
            for i in range(npHess.shape[0]):
              f.write('{0:20.10f}{1:20.10f}{2:20.10f}\n'.format(npHess[i][0],npHess[i][1],npHess[i][2]))
            f.close()
        else:
            raise Exception('Other hessian prog not yet implemented')
